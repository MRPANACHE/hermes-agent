"""Offline native host -> inherited socket -> real unchanged queue/SQLite integration.

Synthetic backend/identity and synthetic native ledger only. No live MCP server,
config/token read, auth refresh, recovery operation or business fact write.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from mcp.types import CallToolResult, TextContent

from tools import mcp_tool as core
from tools.mcp_oauth_manager import HermesMCPOAuthProvider, MCPOAuthManager, _ProviderEntry
from tools import mcp_tool_pax_recovery as r
from hermes_constants import set_hermes_home_override, reset_hermes_home_override

HERE = Path(__file__).parent


class Backend:
    def __init__(self, ready, scenario):
        self.history = ready['history']
        self.fact = ready['fact']
        self.scenario = scenario
        self.calls = []
        self.context = NS(server_url=r.ENDPOINT, client_info=NS(client_id=r.CLIENT),
                          current_tokens=NS(scope='read write'), is_token_valid=lambda: True)
        self.provider = NS(context=self.context, _pax_wire_guard_installed=True)
        self.session = NS(call_tool=self.call_tool)
        self.server = NS(session=self.session, _rpc_lock=asyncio.Lock(), _auth_type='oauth',
                         _config={'url':r.ENDPOINT}, _is_http=lambda:True)
        if scenario == 'unauthorized-reader': self.context.client_info.client_id = 'other-reader'
        if scenario == 'expired-auth': self.context.is_token_valid = lambda: False

    async def call_tool(self, tool, arguments, **kw):
        self.calls.append((tool, arguments, kw))
        # Exercise the native request fence, not a replacement for the reader.
        guard = self.provider._pax_frozen_read
        req = NS(url=r.ENDPOINT, method='POST', content=json.dumps({'method':'tools/call', 'params':{
            'name':tool, 'arguments':arguments, '_meta':kw['meta']}}).encode(), extensions={})
        await guard.guard_request(req)
        if self.scenario == 'timeout': await asyncio.sleep(1)
        if self.scenario == 'connection-change': self.server.session = NS()
        if tool == 'curation_history':
            row = copy.deepcopy(self.history)
            if self.scenario == 'wrong-curator': row['curator_client_id']='not-the-original-curator'
            if self.scenario == 'missing-curator': del row['curator_client_id']
            if self.scenario == 'empty-curator': row['curator_client_id']=''
            if self.scenario == 'request-mismatch': row['request_id']='other'
            if self.scenario == 'source-mismatch': row['source_id']='private'
            if self.scenario == 'partial-history': del row['decisions'][0]['reason']
            if self.scenario == 'decision-mismatch': row['decisions'][0]['reason']='different'
            if self.scenario == 'history-changed' and len(self.calls)>2: row['created_at']='2026-10-28T20:00:00.000Z'
            value={'items':[row], 'next_cursor':None}
            if self.scenario == 'zero-history': value['items']=[]
            if self.scenario == 'multiple-history': value['items'].append(copy.deepcopy(row))
            if self.scenario == 'truncated-history': value['next_cursor']='more'
        else:
            value={'facts':[copy.deepcopy(self.fact)]}
            if self.scenario == 'fact-mismatch': value['facts'][0]['source_session']='other'
        text=json.dumps(value)
        if self.scenario == 'truncated-text': text=text[:30]+'...truncated...'
        return CallToolResult(isError=False, content=[TextContent(type='text',text=text)])


class Composition(unittest.TestCase):
    def run_case(self, scenario):
        left,right=socket.socketpair()
        env={'PATH':'/usr/bin:/bin','HOME':str(HERE), 'TMPDIR':os.environ['TMPDIR']}
        child=subprocess.Popen(['/usr/bin/node',str(HERE/'offline-consumer.mjs'),str(right.fileno()),scenario],
                               pass_fds=(right.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True,env=env)
        right.close()
        line=child.stdout.readline()
        if not line:
            _,err=child.communicate(timeout=20)
            self.fail(err)
        backend=Backend(json.loads(line),scenario)
        manager=MCPOAuthManager()
        manager._entries[(str(r.HOME),'brain')]=_ProviderEntry(r.ENDPOINT,None,backend.provider)
        loop=asyncio.new_event_loop()
        thread=threading.Thread(target=loop.run_forever)
        thread.start()
        errors=[]
        def serve():
            token=set_hermes_home_override(r.HOME)
            try:
                core.serve_pax_recovery_reads(left.detach())
            except BaseException as e: errors.append(str(e))
            finally: reset_hermes_home_override(token)
        from tools.mcp_tool_scope import _server_key
        # Exact owner home in a single-profile test process. No config loader involved.
        with patch.dict(core._servers, {'brain':backend.server}, clear=True), \
             patch.object(core,'_mcp_loop',loop), \
             patch('tools.mcp_tool_pax_recovery.HOME',r.HOME), \
             patch('tools.mcp_oauth_manager.get_manager',return_value=manager), \
             patch('tools.mcp_tool_pax_recovery.TIMEOUT',0.05), \
             patch('tools.mcp_tool_scope._server_key',return_value='brain'):
            host=threading.Thread(target=serve)
            host.start()
            out,err=child.communicate(timeout=25)
            host.join(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        self.assertEqual(child.returncode,0,err)
        self.assertFalse(host.is_alive())
        result=json.loads(out.strip().splitlines()[-1])
        self.assertLessEqual(len(backend.calls),4)
        if scenario == 'success':
            self.assertTrue(result['ok'],(result,errors))
            self.assertEqual(result['job']['status'],'completed')
            self.assertEqual(result['events'],1)
            self.assertNotIn('curation_runtime',result['job'])
            self.assertEqual(result['result']['historical_runtime']['status'],'unknown')
            self.assertEqual([c[0] for c in backend.calls],['curation_history','list_facts']*2)
        else:
            self.assertFalse(result['ok'],result)
            self.assertEqual(result['job']['status'],'failed')
            if scenario not in ['changed-job','changed-raw','history-changed']:
                self.assertEqual(result['events'],0)
        return result,errors,backend

    def test_real_consumer_over_native_channel(self):
        scenarios=['success','wrong-curator','missing-curator','empty-curator','unauthorized-reader',
                   'expired-auth','request-mismatch','source-mismatch','zero-history','multiple-history',
                   'partial-history','truncated-history','truncated-text','decision-mismatch','changed-job',
                   'conflict','timeout','connection-change','fact-mismatch','linked-conflict','changed-raw','history-changed']
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                result,errors,b=self.run_case(scenario)
                print(json.dumps({'scenario':scenario,'offline_only':True,'ok':result['ok'],
                                  'native_calls':len(b.calls),'error':result.get('error'),'host_errors':errors}),flush=True)

    def test_profile_mismatch_before_native_access(self):
        token=set_hermes_home_override(HERE)
        try:
            with self.assertRaisesRegex(r.RecoveryReadError,'profile_mismatch'): r.open_reads()
        finally: reset_hermes_home_override(token)

    def test_native_sdk_result_aliases_and_complete_payload(self):
        value={'items':[], 'next_cursor':None}
        result=CallToolResult(content=[], structuredContent=value)
        self.assertEqual(r.payload(result),value)
        for result,reason in [
            (CallToolResult(content=[TextContent(type='text',text=json.dumps(value))],isError=True),'remote_error'),
            (CallToolResult(content=[TextContent(type='text',text=json.dumps(value))],structuredContent={'other':True}),'content_conflict'),
            (NS(content=[],is_error=False,structured_content=value,result_type='task'),'result_incomplete'),
            (CallToolResult(content=[TextContent(type='text',text='x'*r.MAX_BYTES)]),'content_invalid'),
            (CallToolResult(content=[TextContent(type='text',text='x'*(r.MAX_BYTES+1))]),'content_overflow')]:
            with self.assertRaisesRegex(r.RecoveryReadError,reason):r.payload(result)

    def test_input_cannot_select_tools_identity_or_sources(self):
        async def run():
            for message in [{'op':'history','request_id':'other'},{'op':'history','source':'private'},
                            {'op':'call','tool':'curate_facts'},{'op':'history','history':{}},
                            {'op':'fact','index':0},{'op':'history','identity':r.CLIENT}]:
                b=Backend({'history':{},'fact':{}},'success')
                reader=r.PaxRecoveryReads(b.server,b.provider,lambda:b.server)
                with self.assertRaises(r.RecoveryReadError): await reader.exchange(message)
                self.assertEqual(b.calls,[])
                self.assertTrue(reader.stopped)
        asyncio.run(run())

    def test_frozen_auth_uses_one_request_no_reload_or_refresh(self):
        async def run():
            b=Backend({'history':{},'fact':{}},'success')
            provider=object.__new__(HermesMCPOAuthProvider)
            provider.context=b.context
            provider._add_auth_header=lambda request: None
            provider._pax_frozen_read=r.FrozenPaxAuth(provider,'curation_history',{'request_id':r.REQUEST,'limit':100})
            request=NS(content=b'{}')
            with patch('tools.mcp_oauth_manager.get_manager',side_effect=AssertionError('auth load prohibited')):
                for status in [200,401,403,302,500]:
                    flow=provider.async_auth_flow(request)
                    self.assertIs(await flow.__anext__(),request)
                    with self.assertRaises(StopAsyncIteration if status==200 else r.RecoveryReadError):
                        await flow.asend(NS(status_code=status))
                    await flow.aclose()
                provider._pax_frozen_read=None
                marked=NS(content=json.dumps({'params':{'_meta':{'io.mrpanache/oos-native-session':{'session_id':r.NATIVE_SESSION}}}}).encode())
                with self.assertRaisesRegex(RuntimeError,'no_active_host'): await provider.async_auth_flow(marked).__anext__()
        asyncio.run(run())

    def test_frozen_wire_denies_redirect_retry_write_or_changed_session(self):
        async def run():
            b=Backend({'history':{},'fact':{}},'success')
            for url,tool,session in [(r.ENDPOINT,'curate_facts',r.NATIVE_SESSION),('http://elsewhere/mcp','curation_history',r.NATIVE_SESSION),
                                     (r.ENDPOINT,'curation_history','other')]:
                guard=r.FrozenPaxAuth(b.provider,'curation_history',{'request_id':r.REQUEST,'limit':100})
                request=NS(url=url,method='POST',content=json.dumps({'method':'tools/call','params':{
                    'name':tool,'arguments':{'request_id':r.REQUEST,'limit':100},'_meta':{
                        'io.mrpanache/oos-native-session':{'session_id':session}}}}).encode(),extensions={})
                with self.assertRaises(r.RecoveryReadError): await guard.guard_request(request)
                self.assertTrue(guard.failed)
        asyncio.run(run())


if __name__=='__main__': unittest.main(verbosity=2)

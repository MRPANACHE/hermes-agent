"""Production transport hooks + real SDK HTTPX auth driver, synthetic HTTP only."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tools import mcp_tool as core
from tools import mcp_tool_pax_recovery as r
from tools.mcp_oauth_manager import HermesMCPOAuthProvider
from tools.mcp_tool_transport import MCPServerTransportMixin


class Wire(unittest.TestCase):
    def test_owned_transport_guards_auth_and_every_http_attempt(self):
        core._ensure_mcp_sdk()
        httpx=core.sdk_httpx()
        async def run():
            for status in [200,401,403,302,500]:
                sends=[]
                provider=object.__new__(HermesMCPOAuthProvider)
                provider.context=NS(server_url=r.ENDPOINT, client_info=NS(client_id=r.CLIENT),
                    current_tokens=NS(scope='read write'),is_token_valid=lambda:True)
                provider._add_auth_header=lambda request:None
                args={'request_id':r.REQUEST,'limit':100}
                guard=r.FrozenPaxAuth(provider,'curation_history',args)
                provider._pax_frozen_read=guard
                async def remote(request):
                    sends.append(request)
                    return httpx.Response(status,json={'items':[],'next_cursor':None},
                        headers={'location':'http://elsewhere/mcp'} if status==302 else {})
                def factory(**kwargs):
                    kwargs['transport']=httpx.MockTransport(remote)
                    return httpx.AsyncClient(**kwargs)
                @asynccontextmanager
                async def streams(url,http_client):
                    yield http_client
                server=NS(name='brain',_http_rejection={})
                context=MCPServerTransportMixin._streamable_http_transport
                with patch.object(core,'_MCP_NEW_HTTP',True),patch.object(core,'streamable_http_client',streams), \
                     patch('tools.mcp_tool_transport._make_redirect_header_stripper',return_value=factory), \
                     patch('tools.mcp_oauth_manager.get_manager',side_effect=AssertionError('reload prohibited')):
                    async with context(server,r.ENDPOINT,{},1,True,None,provider,False,set()) as client:
                        self.assertTrue(provider._pax_wire_guard_installed)
                        data={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
                            'name':'curation_history','arguments':args,'_meta':{
                                'io.mrpanache/oos-native-session':{'session_id':r.NATIVE_SESSION}}}}
                        if status==200:
                            response=await client.post(r.ENDPOINT,json=data)
                            self.assertEqual(response.status_code,200)
                            with self.assertRaises(r.RecoveryReadError): await client.post(r.ENDPOINT,json=data)
                        else:
                            with self.assertRaisesRegex(RuntimeError,'transport_status'):
                                await client.post(r.ENDPOINT,json=data)
                        self.assertEqual(len(sends),1)
                        # A delayed queued request after host timeout cannot fall into SDK OAuth.
                        provider._pax_frozen_read=None
                        with self.assertRaisesRegex(RuntimeError,'no_active_host'):
                            await client.post(r.ENDPOINT,json=data)
                        self.assertEqual(len(sends),1)
                    self.assertFalse(provider._pax_wire_guard_installed)
                print(json.dumps({'wire_status':status,'http_sends':len(sends),'refresh':0,'redirect':0,'retry':0}),flush=True)
        asyncio.run(run())

if __name__=='__main__':unittest.main(verbosity=2)

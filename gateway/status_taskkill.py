"""Read back target exit after taskkill reports a process-tree teardown race."""


def require_exit_after_taskkill_error(pid, result):
    """Accept only observed exit; unreadable or still-running targets retain the error.

    The caller has already enforced its original start-time guard and attempted
    taskkill once. A nonzero tree result can name an already-exited descendant;
    allow two seconds for asynchronous kernel teardown. No retry, signalling,
    PID substitution or language-specific error parsing occurs.
    """
    try:
        import psutil
    except ImportError:
        pass
    else:
        try:
            psutil.Process(pid).wait(timeout=2)
        except psutil.NoSuchProcess:
            return
        except psutil.Error:
            pass
        else:
            return
    details = (result.stderr or result.stdout or '').strip()
    raise OSError(details or f'taskkill failed for PID {pid}')

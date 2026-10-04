"""Small stateful systemctl model: stop GC, reload, failure and start limit."""
import subprocess
from types import SimpleNamespace

class SystemdModel:
    def __init__(self, names=()):
        self.states = {n: ('loaded', 'inactive') for n in names}
        self.calls = []
        self.limited = set()
        self.reloads = 0

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        action = args[1]
        names = [x for x in args[2:] if x.endswith(('.service', '.socket'))]
        if action == 'daemon-reload':
            self.reloads += 1
        elif action == 'show':
            load, active = self.states.get(names[0], ('not-found', 'inactive'))
            return SimpleNamespace(stdout=f'LoadState={load}\nActiveState={active}\n'.encode())
        elif action == 'stop':
            for n in names:
                self.states[n] = ('not-found', 'inactive')  # GC after stop
        elif action == 'reset-failed':
            for n in names:
                if self.states.get(n, ('not-found', 'inactive'))[0] == 'not-found':
                    raise subprocess.CalledProcessError(1, args, stderr=b'unit not loaded')
                self.states[n] = ('loaded', 'inactive')
                self.limited.discard(n)
        elif action == 'start':
            for n in names:
                if n in self.limited:
                    self.states[n] = ('loaded', 'failed')
                    raise subprocess.CalledProcessError(1, args)
                self.states[n] = ('loaded', 'active')
        return SimpleNamespace(stdout=b'')

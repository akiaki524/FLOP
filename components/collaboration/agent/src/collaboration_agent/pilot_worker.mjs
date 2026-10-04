// Untrusted Worker process: sibling of Signer, no Gate fd, files, or spawn permission.
import { blockNetwork } from './pilot_protocol.mjs';
blockNetwork();
process.on('message', message => {
  if (message?.kind === 'request') {
    const { id, method, value } = message;
    process.send({ kind: 'workerRequest', id, method, value });
  } else if (message?.kind === 'response') {
    process.send({ kind: 'workerResponse', id: message.id, result: message.result,
      ok: message.ok, code: message.code });
  }
});
process.on('disconnect', () => process.exit(0));
process.send({ kind: 'ready', pid: process.pid, parentPid: process.ppid,
  canSpawn: process.permission.has('child') });

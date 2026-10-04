import {
  closeSync,
  fsyncSync,
  openSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}

export function durabilityProbe(directory) {
  if (typeof directory !== 'string' || !directory.startsWith('/')) {
    fail('DURABILITY_PROBE_DIRECTORY');
  }
  const base = 'probe-' + process.pid;
  const temp = join(directory, '.' + base + '.tmp');
  const finalPath = join(directory, base + '.json');
  let fd;
  let dfd;
  try {
    fd = openSync(temp, 'wx', 0o600);
    writeFileSync(fd, Buffer.from('{"ok":true}\n', 'utf8'));
    fsyncSync(fd);
    closeSync(fd);
    fd = undefined;

    renameSync(temp, finalPath);
    dfd = openSync(directory, 'r');
    fsyncSync(dfd);
    closeSync(dfd);
    dfd = undefined;

    unlinkSync(finalPath);
    dfd = openSync(directory, 'r');
    fsyncSync(dfd);
    closeSync(dfd);
    dfd = undefined;

    return { ok: true, fileFsync: true, directoryFsync: true };
  } catch (error) {
    if (fd !== undefined) try { closeSync(fd); } catch {}
    if (dfd !== undefined) try { closeSync(dfd); } catch {}
    try { unlinkSync(temp); } catch {}
    try { unlinkSync(finalPath); } catch {}
    const wrapped = new Error('DURABILITY_PROBE_FAILED');
    wrapped.code = 'DURABILITY_PROBE_FAILED';
    wrapped.cause = error;
    throw wrapped;
  }
}

function main() {
  if (process.argv.length !== 3) fail('ARGUMENTS_DENIED');
  process.stdout.write(JSON.stringify(durabilityProbe(process.argv[2])) + '\n');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); }
  catch (error) {
    process.stderr.write((error?.code ?? 'DURABILITY_PROBE_FAILED') + '\n');
    process.exitCode = 70;
  }
}

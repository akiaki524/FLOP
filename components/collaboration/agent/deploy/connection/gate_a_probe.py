"""Gate A ExecStartPre measurement only; accepts exclusively the fixed public fake seed.

No CLI input, identity override, signing, network or plaintext persistence.
The production Signer still independently reads and rejects this seed.
"""
import base64
import errno
import json
import os
from pathlib import Path
import pwd
import stat
import struct
import time

# Linux POSIX ACL xattr (v2): u32 version, then (u16 tag, u16 perm, u32 id) entries.
ACL_XATTR = 'system.posix_acl_access'
ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_GROUP, ACL_MASK, ACL_OTHER = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
ACL_UNDEFINED_ID = 0xFFFFFFFF


def acl_entries(path):
    """Parsed access ACL, or None when the file carries no extended ACL."""
    try:
        blob = os.getxattr(path, ACL_XATTR)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.EOPNOTSUPP):
            return None
        raise
    if len(blob) < 4 or (len(blob) - 4) % 8 or struct.unpack_from('<I', blob)[0] != 2:
        raise ValueError('ACL_MALFORMED')
    return [list(struct.unpack_from('<HHI', blob, 4 + 8 * i)) for i in range((len(blob) - 4) // 8)]


def only_owner_and_service(mode, entries, service_uid):
    """True when no principal other than the file owner and the service UID has access.

    systemd grants a non-root service read access to its credentials through a named-user
    ACL entry on a root-owned 0400 file. With an ACL present, the stat group bits report
    the ACL mask, not the owning group's permission, so they are not evidence of access.
    """
    if entries is None:
        return stat.S_IMODE(mode) & 0o077 == 0
    tags = [tag for tag, _, _ in entries]
    if any(tags.count(tag) != 1 for tag in (ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_OTHER)) or tags.count(ACL_MASK) > 1:
        return False
    for tag, perm, uid in entries:
        if tag == ACL_USER and (uid != service_uid or perm & ~0o4):
            return False
        if tag in (ACL_GROUP_OBJ, ACL_GROUP, ACL_OTHER) and perm:
            return False
        if tag not in (ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_GROUP, ACL_MASK, ACL_OTHER):
            return False
    return True


def metadata(path):
    info = os.stat(path)
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': oct(stat.S_IMODE(info.st_mode)),
            'acl': acl_entries(path)}


def main():
    seed = bytes(range(32))
    credential = Path(os.environ['CREDENTIALS_DIRECTORY']) / 'project-seed'
    raw = credential.read_bytes()
    status = Path('/proc/self/status').read_text()
    forms = (seed, seed.hex().encode(), base64.b64encode(seed),
             base64.urlsafe_b64encode(seed).rstrip(b'='))
    process_metadata = Path('/proc/self/environ').read_bytes() + Path('/proc/self/cmdline').read_bytes()
    info = credential.stat()
    # Permission metadata only (numeric ids, mode, ACL entries); never content.
    observed = {str(p): metadata(p) for p in (credential, credential.parent, credential.parent.parent)}
    result = {
        'fake_credential_exact': raw == seed,
        'credential_32_bytes': len(raw) == 32,
        'uid_matches': os.getuid() == pwd.getpwnam('collab-signer').pw_uid,
        'credential_owner_allowed': info.st_uid in (0, os.getuid()),
        'credential_other_permissions_absent': only_owner_and_service(
            info.st_mode, observed[str(credential)]['acl'], os.getuid()),
        'no_new_privileges': 'NoNewPrivs:\t1' in status,
        'seed_absent_env_argv': all(value not in process_metadata for value in forms),
    }
    raw = None
    result['pid'] = os.getpid()
    result['invocation_id'] = os.environ.get('INVOCATION_ID')
    result['credential_metadata'] = observed
    path = Path('/var/lib/collab-signer/gate-a-probe.json')
    temporary = path.with_name('gate-a-probe.pending')
    with temporary.open('x') as stream:
        os.chmod(temporary, 0o600)
        json.dump(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if not all(value is True for key, value in result.items()
               if key not in ('pid', 'invocation_id', 'credential_metadata')):
        raise SystemExit(1)
    # Bounded observation window for root's cross-UID DAC measurements.
    time.sleep(5)


if __name__ == '__main__':
    main()

"""Human-only staged deployment packet. Import has no side effects. No Stage executes another Stage."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from policy import (ETC, GIB, IMAGES, INSTALL, MIB, NOTIFY_ETC, POLICY, ROOT, VOLUMES,
                    STANDING_CLASSIFICATION, decision_template, digest, draft, require)

REPO = HERE.parents[1]
BASE_IMAGE = "python:3.12-slim@sha256:" + (
    "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
E = Path(ETC)


def run(argv, *, capture=False, timeout=300):
    return subprocess.run([str(x) for x in argv], check=True, capture_output=capture,
                          text=True, timeout=timeout,
                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})


def read_json(path):
    require(not Path(path).is_symlink(), "JSON_SYMLINK")
    return json.loads(Path(path).read_text())


def write_new(path, content, mode=0o644):
    if not isinstance(content, bytes): content = content.encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content); stream.flush(); os.fsync(stream.fileno())
    sync_dir(Path(path).parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def mkdir_new(path, *, private=False):
    Path(path).mkdir(mode=0o700 if private else 0o755)
    os.chmod(path, 0o700 if private else 0o755)


def save(name, obj):
    write_new(E / name, json.dumps(obj, sort_keys=True, indent=2) + "\n")


def bundle_hash():
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(HERE.iterdir()) if p.suffix in (".py", ".sh", ".json")})


def source_check(root=REPO):
    manifest = read_json(HERE / "source-manifest.json")
    for relative, expected in manifest.items():
        p = Path(root) / relative
        require(p.resolve() == p and p.is_file(), "SOURCE_PATH")
        require(hashlib.sha256(p.read_bytes()).hexdigest() == expected, "SOURCE_HASH_CHANGED")
    return manifest


def receipt(name, *, max_age=None):
    obj = read_json(E / name)
    require(obj["bundle_sha256"] == bundle_hash(), "PACKET_CHANGED_SINCE_GATE")
    if max_age is not None:
        require(0 <= time.time() - obj["observed_at"] <= max_age, "GATE_EVIDENCE_STALE")
    return obj


def mark(name, **extra):
    save(name, {"bundle_sha256": bundle_hash(), "observed_at": time.time(), **extra})


def require_host():
    require(os.geteuid() == 0 and socket.gethostname() == "node-01", "HUMAN_ROOT_NODE01_REQUIRED")


def backing_check():
    rows = json.loads(run([
        "findmnt", "--json", "--mountpoint", "/srv/technocore-data",
        "--output", "SOURCE,FSTYPE,TARGET"
    ], capture=True).stdout)["filesystems"]
    require(len(rows) == 1 and rows[0]["source"] == "/dev/sdb"
            and rows[0]["fstype"] == "ext4", "BACKING_MOUNT_CHANGED")


def unused_loops():
    rows = json.loads(run([
        "losetup", "--list", "--json", "--output", "NAME,BACK-FILE"
    ], capture=True).stdout)["loopdevices"]
    require(not any(x["name"] in {"/dev/loop40", "/dev/loop41", "/dev/loop42"}
                    for x in rows), "FIXED_LOOP_IN_USE")


def mount_unit(name):
    return "srv-technocore\\x2dcapture-" + name + ".mount"


def mounted_check():
    backing_check()
    for name, (number, size) in VOLUMES.items():
        p = Path(ROOT) / name
        require(p.resolve() == p and os.path.ismount(p), "MISSING_MOUNT")
        require(p.stat().st_dev == os.stat("/dev/loop" + str(number)).st_rdev, "LOOP_DEVICE_CHANGED")
        backing = Path(f"/sys/block/loop{number}/loop/backing_file").read_text().strip()
        require(backing == IMAGES + "/" + name + ".img", "LOOP_BACKING_CHANGED")
        s = Path(backing).stat()
        require(s.st_size == size and s.st_blocks*512 >= size, "ALLOCATION_LOST")
    saved = E / "30-mount-identity.json"
    if saved.exists():
        old = receipt(saved.name)
        current = identities()
        for path, row in old["paths"].items():
            require(all(row[k] == current[path][k] for k in ("device", "inode", "uid", "mode")),
                    "MOUNT_IDENTITY_CHANGED")


def identities():
    out = {}
    for part in ("spool/data", "archive/data", "control/registry", "control/budget"):
        p = Path(ROOT) / part
        s, v = p.stat(), os.statvfs(p)
        out[part] = {"device": s.st_dev, "inode": s.st_ino, "uid": s.st_uid,
                     "mode": stat.S_IMODE(s.st_mode), "total": v.f_blocks*v.f_frsize,
                     "available": v.f_bavail*v.f_frsize, "inodes": v.f_files,
                     "free_inodes": v.f_ffree, "available_inodes": v.f_favail}
    return out


def stage00(args):
    from readonly import collect
    if args.print_decision_template:
        print(json.dumps(decision_template(), indent=2)); return 0
    require_host()
    decisions = read_json(args.decisions) if args.decisions else None
    report = collect(decisions, include_public_ip=args.public_ip)
    report["bundle_sha256"] = bundle_hash()
    # stdout only. Human can save/export this report outside this read-only Stage.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


def stage10(args):
    require(args.stage00_evidence, "STAGE00_EVIDENCE_REQUIRED")
    report = read_json(args.stage00_evidence)
    require(report.get("stage") == "00" and report.get("verdict") == "PASS", "STAGE00_NOT_PASS")
    require(report.get("blockers") == [] and report.get("human_gates") == [], "STAGE00_OPEN_GATES")
    require(report.get("host") == socket.gethostname(), "STAGE00_HOST_MISMATCH")
    require(report.get("bundle_sha256") == bundle_hash(), "STAGE00_PACKET_MISMATCH")
    require(0 <= time.time() - report["observed_at"] <= 3600, "STAGE00_STALE")
    backing_check(); unused_loops()
    v = os.statvfs("/srv/technocore-data")
    require(v.f_bavail*v.f_frsize >= 20736*MIB, "PARENT_CAPACITY")
    require(not E.exists() and not Path(IMAGES).exists(), "PROVISION_PATH_ALREADY_EXISTS")
    mkdir_new(E)
    save("00-evidence.json", report)
    mkdir_new(IMAGES, private=True)
    for name, (_, size) in VOLUMES.items():
        p = Path(IMAGES) / (name + ".img")
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try: os.posix_fallocate(fd, 0, size); os.fsync(fd)
        finally: os.close(fd)
        run(["mkfs.ext4", "-F", "-m", "0", "-i", "4096", "-L", "tc-" + name,
             "-E", "nodiscard,lazy_itable_init=0,lazy_journal_init=0", p])
        s = p.stat()
        require(s.st_size == size and s.st_blocks*512 >= size, "IMAGE_NOT_PREALLOCATED")
        print(json.dumps({"volume": name, "bytes": size, "allocated": s.st_blocks*512}))
    mark("10-provision.json", verdict="PASS")


def stage20(args):
    receipt("10-provision.json"); backing_check(); unused_loops()
    mkdir_new(ROOT)
    lib = Path("/usr/local/libexec")
    if not lib.exists(): mkdir_new(lib)
    write_new(lib / "technocore-capture-loop.py", (HERE / "loop_mount.py").read_bytes(), 0o700)
    write_new("/etc/systemd/system/technocore-capture-loop@.service", """[Unit]
Description=Technocore fixed loop device %i
DefaultDependencies=no
RequiresMountsFor=/srv/technocore-data
After=local-fs-pre.target
Before=srv-technocore\\x2dcapture-%i.mount
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/technocore-capture-loop.py %i
TimeoutStartSec=300
""")
    for name, (number, _) in VOLUMES.items():
        target = ROOT + "/" + name
        mkdir_new(target)
        write_new("/etc/systemd/system/" + mount_unit(name), f"""[Unit]
Description=Technocore bounded {name}
Requires=technocore-capture-loop@{name}.service
After=technocore-capture-loop@{name}.service
RequiresMountsFor=/srv/technocore-data
[Mount]
What=/dev/loop{number}
Where={target}
Type=ext4
Options=rw,nosuid,nodev,noexec,nodiscard
TimeoutSec=60
[Install]
WantedBy=local-fs.target
""")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", *[mount_unit(n) for n in VOLUMES]])
    mounted_check()
    mark("20-mounts.json", verdict="PASS")


def stage30(args):
    receipt("20-mounts.json"); mounted_check()
    for part in ("spool/data", "archive/data", "control/registry", "control/budget",
                 "spool/probe", "archive/probe", "control/probe"):
        p = Path(ROOT) / part
        mkdir_new(p, private=True); os.chown(p, 65532, 65532)
    backup = "/srv/technocore-data/production-capture-backup-01"
    mkdir_new(backup, private=True); os.chown(backup, 65532, 65532)
    mkdir_new(Path(ROOT) / "control/observation", private=True)
    for name in ("outbox", "sent", "failed"):
        path = Path(ROOT) / "control/notification" / name
        path.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        mkdir_new(path, private=True)
    before = identities()
    for name, (number, _) in VOLUMES.items():
        run(["systemctl", "stop", mount_unit(name)])
        run(["systemctl", "stop", "technocore-capture-loop@" + name + ".service"])
        run(["losetup", "--detach", "/dev/loop" + str(number)])
        run(["systemctl", "start", mount_unit(name)])
    after = identities()
    for part in before:
        require(all(before[part][k] == after[part][k] for k in ("device", "inode", "uid", "mode")),
                "REMOUNT_IDENTITY_CHANGED")
        require(after[part]["uid"] == 65532 and after[part]["mode"] == 0o700, "OWNERSHIP")
    mark("30-mount-identity.json", paths=after, verdict="PASS", cycle="detach/reattach; no host reboot")
    print(json.dumps(after, indent=2))


def stage40(args):
    receipt("30-mount-identity.json"); mounted_check()
    manifest = source_check()
    report = read_json(E / "00-evidence.json")
    mkdir_new(INSTALL)
    files = [(REPO / name, Path(INSTALL) / name) for name in manifest]
    files += [(p, Path(INSTALL) / "deploy/production-capture-human" / p.name)
              for p in HERE.iterdir() if p.suffix in (".py", ".sh", ".json")]
    for src, target in files:
        target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        parent = target.parent
        while parent != Path(INSTALL):
            os.chmod(parent, 0o755)
            parent = parent.parent
        write_new(target, src.read_bytes(), 0o644)
    config = draft(report["human_attestation"]["clients"], continuous=(
        report["human_attestation"]["egress"]["classification"] == STANDING_CLASSIFICATION))
    save("policy.json", config)  # Still null window, unusable for Production run.
    save("pilot-policy.json", POLICY)
    save("egress.json", report["human_attestation"]["egress"])
    mkdir_new(NOTIFY_ETC, private=True)
    for role in ("capture", "archive"):
        write_new("/etc/systemd/system/technocore-capture-" + role + ".service", f"""[Unit]
Description=Technocore independent {role}
Requires=docker.service
After=docker.service
RequiresMountsFor={ROOT}/spool {ROOT}/archive {ROOT}/control
[Service]
Type=simple
ExecStartPre=/usr/bin/python3 -I -B {INSTALL}/deploy/production-capture-human/packet.py check
ExecStart=/usr/bin/docker start --attach tc-cap-loop-01-lobby-{role}
ExecStop=/usr/bin/docker stop --time 40 tc-cap-loop-01-lobby-{role}
TimeoutStopSec=50
Restart=no
StandardOutput=null
StandardError=journal
""")
    write_new("/etc/systemd/system/technocore-capture-monitor.service", f"""[Unit]
Description=Technocore local observation and checkpoint
RequiresMountsFor={ROOT}/control
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B {INSTALL}/deploy/production-capture-human/monitor.py
TimeoutStartSec=180
MemoryMax=128M
CPUQuota=10%
Nice=10
NoNewPrivileges=true
""")
    write_new("/etc/systemd/system/technocore-capture-monitor.timer", """[Unit]
Description=Technocore one-minute observation
[Timer]
OnActiveSec=1s
OnUnitActiveSec=60s
AccuracySec=5s
[Install]
WantedBy=timers.target
""")
    write_new("/etc/systemd/system/technocore-capture-notifier.service", f"""[Unit]
Description=Technocore Capture STOP Discord notifier
RequiresMountsFor={ROOT}/control
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B {INSTALL}/deploy/production-capture-human/notifier.py
TimeoutStartSec=30
MemoryMax=64M
CPUQuota=5%
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths={ROOT}/control/notification
ReadOnlyPaths={NOTIFY_ETC}
""")
    write_new("/etc/systemd/system/technocore-capture-notifier.timer", """[Unit]
Description=Technocore Capture STOP notification delivery
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s
Persistent=true
[Install]
WantedBy=timers.target
""")
    run(["systemctl", "daemon-reload"])
    mark("40-install.json", draft_sha256=digest(config), verdict="PASS")


def device_map():
    result = {}
    for name, path in {"backing": "/dev/sdb", **{n: "/dev/loop" + str(v[0]) for n, v in VOLUMES.items()}}.items():
        s = os.stat(path)
        require(stat.S_ISBLK(s.st_mode), "NOT_BLOCK_DEVICE")
        result[name] = str(os.major(s.st_rdev)) + ":" + str(os.minor(s.st_rdev))
    return result


def recheck_egress():
    from readonly import Reader, firewall_evidence
    egress = read_json(E / "egress.json")
    now = firewall_evidence(Reader())
    require(digest(now) == egress["firewall_evidence_sha256"], "EGRESS_RULES_CHANGED_REVIEW_REQUIRED")
    return egress


def profile(role, network):
    require(role in ("capture", "archive"), "ROLE")
    memory = "256m" if role == "capture" else "384m"
    a = ["--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--pids-limit", "32", "--cpus", "0.5",
         "--memory", memory, "--memory-swap", memory, "--restart", "no",
         "--stop-timeout", "40", "--network", network, "--cgroupns", "private",
         "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,mode=700,uid=65532,gid=65532",
         "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3"]
    for dev in ("/dev/sdb", "/dev/loop40", "/dev/loop41", "/dev/loop42"):
        for flag, value in (("--device-read-bps", "10mb"), ("--device-write-bps", "10mb"),
                            ("--device-read-iops", "200"), ("--device-write-iops", "200")):
            a += [flag, dev + ":" + value]
    return a


def prod_mounts(role):
    a = ["--mount", "type=bind,src=" + ETC + "/policy.json,dst=/policy.json,readonly"]
    for part, ro in (("spool/data", False), ("archive/data", role == "capture"),
                     ("control/registry", False), ("control/budget", role == "archive")):
        path = ROOT + "/" + part
        a += ["--mount", "type=bind,src=" + path + ",dst=" + path + (",readonly" if ro else "")]
    return a


def image_id():
    value = (E / "image-id").read_text().strip()
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", value), "IMAGE_ID")
    return value


def container_argv(role, network, image):
    return ["docker", "create", "--name", "tc-cap-loop-01-lobby-" + role,
            *profile(role, network if role == "capture" else "none"), *prod_mounts(role),
            "--entrypoint", "python3", image, "-I", "-B", "-m",
            "technocore_full_capture.production", "run", "--config", "/policy.json",
            "--room", "lobby", "--role", role]


def inspect_container(name):
    # Full inspect remains in memory; no Env/health output/log options is displayed or retained.
    x = json.loads(run(["docker", "inspect", name], capture=True).stdout)[0]
    h = x["HostConfig"]
    return {"id": x["Id"], "image": x["Image"], "user": x["Config"]["User"],
            "entrypoint": x["Config"]["Entrypoint"], "cmd": x["Config"]["Cmd"],
            "mounts": [{k: m.get(k) for k in ("Source", "Destination", "RW", "Type")} for m in x["Mounts"]],
            "limits": {k: h.get(k) for k in (
                "ReadonlyRootfs", "Privileged", "CapDrop", "SecurityOpt", "PidsLimit",
                "NanoCpus", "Memory", "MemorySwap", "RestartPolicy", "NetworkMode", "CgroupnsMode",
                "PidMode", "IpcMode", "Devices", "Binds", "CapAdd", "Tmpfs", "BlkioDeviceReadBps", "BlkioDeviceWriteBps",
                "BlkioDeviceReadIOps", "BlkioDeviceWriteIOps")},
            "running": x["State"]["Running"], "pid": x["State"]["Pid"],
            "status": x["State"]["Status"], "exit_code": x["State"]["ExitCode"]}


def check_container(x, role, image, network):
    h = x["limits"]
    memory = (256 if role == "capture" else 384)*MIB
    expected = {"ReadonlyRootfs": True, "Privileged": False, "PidsLimit": 32,
                "NanoCpus": 500000000, "Memory": memory, "MemorySwap": memory,
                "CgroupnsMode": "private", "NetworkMode": network if role == "capture" else "none"}
    require(all(h.get(k) == v for k, v in expected.items()), "CONTAINER_LIMITS_MISMATCH")
    require(h.get("CapDrop") == ["ALL"] and not h.get("CapAdd"), "CONTAINER_CAPABILITIES")
    require("no-new-privileges" in (h.get("SecurityOpt") or []), "CONTAINER_NO_NEW_PRIVILEGES")
    require(not h.get("Devices") and not h.get("Binds") and not h.get("PidMode")
            and h.get("IpcMode") == "private", "CONTAINER_UNEXPECTED_AUTHORITY")
    require(h["RestartPolicy"]["Name"] == "no", "CONTAINER_RESTART_POLICY")
    require(x["image"] == image and x["user"] == "65532:65532", "CONTAINER_IMAGE_OR_USER")
    require(x["entrypoint"] == ["python3"], "CONTAINER_ENTRYPOINT")
    expected_cmd = ["-I", "-B", "-m", "technocore_full_capture.production", "run",
                    "--config", "/policy.json", "--room", "lobby", "--role", role]
    require(x["cmd"] == expected_cmd, "CONTAINER_COMMAND")
    mounts = {m["Destination"]: (m["Source"], m["RW"]) for m in x["mounts"] if m["Type"] == "bind"}
    expected_mounts = {"/policy.json": (ETC + "/policy.json", False)}
    for part, rw in (("spool/data", True), ("archive/data", role == "archive"),
                     ("control/registry", True), ("control/budget", role == "capture")):
        path = ROOT + "/" + part
        expected_mounts[path] = (path, rw)
    require(mounts == expected_mounts, "CONTAINER_MOUNT_ALLOWLIST")
    require(all(m["Type"] == "bind" or (m["Type"] == "tmpfs" and m["Destination"] == "/tmp")
                for m in x["mounts"]), "CONTAINER_EXTRA_MOUNT")
    for field, rate in (("BlkioDeviceReadBps", 10*MIB), ("BlkioDeviceWriteBps", 10*MIB),
                        ("BlkioDeviceReadIOps", 200), ("BlkioDeviceWriteIOps", 200)):
        require({d["Path"]: d["Rate"] for d in h[field]} == {
            dev: rate for dev in ("/dev/sdb", "/dev/loop40", "/dev/loop41", "/dev/loop42")}, "CONTAINER_IO_LIMITS")


def managed_container(argv, *, timeout=240):
    """Own the exact returned ID; stop children even if the attached CLI times out."""
    cid = run(argv, capture=True).stdout.strip()
    require(re.fullmatch(r"[0-9a-f]{64}", cid), "ONE_SHOT_ID")
    try:
        result = run(["docker", "start", "--attach", cid], capture=True, timeout=timeout)
    finally:
        run(["docker", "stop", "--time", "40", cid], capture=True, timeout=60)
    x = inspect_container(cid)
    require(not x["running"] and x["pid"] == 0 and x["exit_code"] == 0, "ONE_SHOT_FAILED")
    return result


def one_shot(command, *, budget_write=False, extra=None):
    a = ["docker", "create", "--name", "tc-cap-loop-01-admin-" + command,
         *profile("archive", "none"), *prod_mounts("archive")]
    if budget_write:
        path = ROOT + "/control/budget"
        a = [x.replace("dst=" + path + ",readonly", "dst=" + path) for x in a]
    if command == "backup":
        dest = "/srv/technocore-data/production-capture-backup-01"
        a += ["--mount", "type=bind,src=" + dest + ",dst=" + dest]
        extra = ["--destination", dest]
    a += ["--entrypoint", "python3", image_id(), "-I", "-B", "-m",
          "technocore_full_capture.production", command, "--config", "/policy.json"]
    if extra: a += extra
    result = managed_container(a)
    value = json.loads(result.stdout)
    print(json.dumps(value))
    return value


STORAGE_PROBE = """
import json, os
from pathlib import Path
from technocore_full_capture import production as p
c=p.load('/policy.json')
assert c['start_at'] is None and c['end_at'] is None
for key in ('control_dir','budget_dir'):
    p.private_directory(c[key])
    assert not any(Path(c[key]).iterdir())
p.control_capacity(c,active_budget=True)
v=os.statvfs(c['control_dir'])
assert v.f_bavail*v.f_frsize >= 56*p.MIB
out={'binding':p.binding(c),'volumes':{}}
for r in c['rooms']:
    for role,key in [('capture','spool_dir'),('archive','archive_dir')]:
        p.private_directory(r[key])
        assert not any(Path(r[key]).iterdir())
        x=p.storage_probe(r,role)
        kind='spool' if role=='capture' else 'archive'
        assert x['disk_free_bytes'] >= p.capacity_model(r)[kind+'_required_bytes']
        out['volumes'][role]=x
print(json.dumps(out))
"""


def stage50(args):
    old = receipt("40-install.json"); mounted_check(); source_check(Path(INSTALL))
    c = read_json(E / "policy.json")
    require(c["start_at"] is None and digest(c) == old["draft_sha256"], "DRAFT_CHANGED")
    egress = recheck_egress()
    network = json.loads(run(["docker", "network", "inspect", egress["network"]], capture=True).stdout)[0]
    require(network["Id"] == egress["network_id"], "NETWORK_ID_CHANGED")
    require(not (E / "image-id").exists(), "STAGE50_ALREADY_ATTEMPTED_REVIEW_PARTIAL_STATE")
    run(["docker", "image", "inspect", BASE_IMAGE], capture=True)
    run(["docker", "build", "--pull=false", "--network=none", "--file",
         INSTALL + "/deploy/Containerfile.full-capture", "--iidfile", E / "image-id", INSTALL], timeout=600)
    image = image_id()
    mounts = [x + ",readonly" if x.startswith("type=bind,") and not x.endswith(",readonly")
              else x for x in prod_mounts("archive")]
    a = ["docker", "create", "--name", "tc-cap-loop-01-storage-probe",
         *profile("archive", "none"), *mounts,
         "--entrypoint", "python3", image, "-I", "-B", "-c", STORAGE_PROBE]
    storage = json.loads(managed_container(a).stdout)
    s = os.stat(ROOT + "/control/budget")
    require(storage["binding"]["budget_device"] == s.st_dev
            and storage["binding"]["budget_inode"] == s.st_ino, "CONTAINER_BINDING_CHANGED")
    mapping = device_map()
    probes = {}
    for role in ("capture", "archive"):
        # The probe sees separate empty scratch directories on the same filesystems.
        # It cannot read/write Production data, budget, or identity directories.
        probe_name = "tc-cap-loop-01-resource-probe-" + role
        a = ["docker", "create", "--name", probe_name, *profile(role, "none")]
        for name in VOLUMES:
            a += ["--mount", "type=bind,src=" + ROOT + "/" + name + "/probe,dst=/probe-" + name]
        a += ["--mount", "type=bind,src=" + str(HERE / "runtime_probe.py") + ",dst=/probe.py,readonly",
              "--entrypoint", "python3", image, "-I", "-B", "/probe.py", role, json.dumps(mapping)]
        result = managed_container(a)
        probes[role] = json.loads(result.stdout)
        require(probes[role].get("verdict") == "PASS", "RUNTIME_PROBE_NOT_PASS")
        for name in VOLUMES:
            require(not any((Path(ROOT) / name / "probe").iterdir()), "PROBE_LEFT_FILES")
    containers = {}
    for role in ("capture", "archive"):
        run(container_argv(role, egress["network"], image))
        x = inspect_container("tc-cap-loop-01-lobby-" + role)
        check_container(x, role, image, egress["network"])
        require(x["status"] == "created" and not x["running"], "CONTAINER_STARTED_EARLY")
        containers[role] = x
    mark("50-preflight.json", verdict="PASS", storage=storage, resource_probes=probes,
         draft_sha256=digest(c), containers=containers, image=image, devices=mapping,
         egress_sha256=digest(egress))
    print(json.dumps({"verdict": "PASS", "resource_probes": probes, "storage": storage}, indent=2))


def stage60(args):
    gate = receipt("50-preflight.json", max_age=3600); mounted_check()
    require(gate["verdict"] == "PASS" and gate["image"] == image_id(), "STAGE50_NOT_PASS")
    require(set(gate["resource_probes"]) == {"capture", "archive"}
            and all(x["verdict"] == "PASS" for x in gate["resource_probes"].values()), "RESOURCE_GATE")
    c = read_json(E / "policy.json")
    require(c["start_at"] is None and digest(c) == gate["draft_sha256"], "WINDOW_ALREADY_SEALED_OR_CONFIG_CHANGED")
    egress = recheck_egress()
    require(digest(egress) == gate["egress_sha256"], "EGRESS_CHANGED")
    network = json.loads(run(["docker", "network", "inspect", egress["network"]], capture=True).stdout)[0]
    require(network["Id"] == egress["network_id"], "NETWORK_CHANGED")
    for role in ("capture", "archive"):
        require(inspect_container("tc-cap-loop-01-lobby-" + role) == gate["containers"][role], "CONTAINER_CHANGED")
    sys.path.insert(0, INSTALL + "/src")
    from technocore_full_capture import production as p
    c["start_at"] = time.time()
    c["end_at"] = None if c["version"] == 2 else c["start_at"] + 86400
    p.validate_config(c)
    write_new(E / "policy.sealed.pending", json.dumps(c, indent=2) + "\n")
    os.replace(E / "policy.sealed.pending", E / "policy.json"); sync_dir(E)
    print(json.dumps({"start_at": c["start_at"], "end_at": c["end_at"], "config_sha256": p.digest(p.canonical(c))}))
    one_shot("initialize", budget_write=True)
    ready = one_shot("recover-archive", extra=["--room", "lobby"])
    require(ready.get("manifest_health") == "READY", "ARCHIVE_NOT_READY")
    one_shot("backup")
    one_shot("verify-restore")
    capture = "technocore-capture-capture.service"
    run(["systemctl", "start", capture])
    metrics = Path(ROOT) / "control/registry/lobby.capture.metrics.json"
    for _ in range(45):
        x = inspect_container("tc-cap-loop-01-lobby-capture")
        if not x["running"]:
            raise ValueError("CAPTURE_EXITED_NO_BLIND_RESTART")
        if metrics.exists() and read_json(metrics).get("service_status") in ("STARTING", "RUNNING"):
            break
        time.sleep(1)
    else:
        run(["systemctl", "stop", capture])
        raise ValueError("CAPTURE_STARTUP_METRICS_TIMEOUT")
    run(["systemctl", "start", "technocore-capture-archive.service"])
    mark("60-started.json", start_at=c["start_at"], end_at=c["end_at"],
         observation_checkpoint_at=c["start_at"] + 86400, pilot=POLICY)
    run(["systemctl", "enable", "--now", "technocore-capture-monitor.timer"])
    run(["systemctl", "enable", "--now", "technocore-capture-notifier.timer"])
    # Status may legitimately be incomplete until Archive publishes its first snapshot.
    for _ in range(10):
        if (Path(ROOT) / "control/registry/lobby.archive.metrics.json").exists(): break
        time.sleep(1)
    one_shot("status")


def stop_roles(roles):
    for role in roles:
        unit = "technocore-capture-" + role + ".service"
        name = "tc-cap-loop-01-lobby-" + role
        run(["systemctl", "stop", unit])
        x = inspect_container(name)
        if x["running"]: run(["docker", "stop", "--time", "40", name], timeout=60)
        x = inspect_container(name)
        require(not x["running"] and x["pid"] == 0, "CONTAINER_NOT_EXITED")
        print(json.dumps({"container": name, "status": x["status"], "exit_code": x["exit_code"]}))


def stage90(args):
    stop_roles(("capture",) if args.capture_only else ("capture", "archive"))


def stage99(args):
    require(args.writers_quiesced, "HUMAN_MUST_CONFIRM_ALL_WRITERS_QUIESCED")
    if Path("/etc/systemd/system/technocore-capture-notifier.timer").exists():
        run(["systemctl", "disable", "--now", "technocore-capture-notifier.timer"])
        run(["systemctl", "stop", "technocore-capture-notifier.service"])
    if Path("/etc/systemd/system/technocore-capture-monitor.timer").exists():
        run(["systemctl", "disable", "--now", "technocore-capture-monitor.timer"])
        run(["systemctl", "stop", "technocore-capture-monitor.service"])
    # Valid after partial provisioning too; do not assume config or containers exist.
    for role in ("capture", "archive"):
        unit = Path("/etc/systemd/system/technocore-capture-" + role + ".service")
        if unit.exists(): run(["systemctl", "stop", unit.name])
    rows = run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture=True).stdout.splitlines()
    for name in rows:
        if name.startswith("tc-cap-loop-01-"):
            x = inspect_container(name)
            require(not x["running"] and x["pid"] == 0, "PACKET_CONTAINER_NOT_STOPPED")
    for role in ("capture", "archive"):
        name = "tc-cap-loop-01-resource-probe-" + role
        if name in rows:
            x = inspect_container(name)
            require(not x["running"] and x["pid"] == 0, "PROBE_NOT_STOPPED")
    for role in ("capture", "archive"):
        name = "tc-cap-loop-01-lobby-" + role
        if name in rows:
            x = inspect_container(name)
            require(not x["running"] and x["pid"] == 0, "WRITER_NOT_STOPPED_USE_STAGE90")
    for name, (number, _) in VOLUMES.items():
        unit = Path("/etc/systemd/system") / mount_unit(name)
        if unit.exists(): run(["systemctl", "disable", "--now", unit.name])
        loopunit = Path("/etc/systemd/system/technocore-capture-loop@.service")
        if loopunit.exists(): run(["systemctl", "stop", "technocore-capture-loop@" + name + ".service"])
        backing = Path(f"/sys/block/loop{number}/loop/backing_file")
        if backing.exists():
            require(backing.read_text().strip() == IMAGES + "/" + name + ".img", "FOREIGN_LOOP_DO_NOT_DETACH")
            require(not os.path.ismount(ROOT + "/" + name), "STILL_MOUNTED")
            run(["losetup", "--detach", "/dev/loop" + str(number)])
    print(json.dumps({"rollback": "PRESERVED", "deleted_files": 0, "released_preallocation": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("00", "10", "20", "30", "40", "50", "60", "90", "99", "check"))
    parser.add_argument("--decisions")
    parser.add_argument("--print-decision-template", action="store_true")
    parser.add_argument("--public-ip", action="store_true")
    parser.add_argument("--stage00-evidence")
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--writers-quiesced", action="store_true")
    args = parser.parse_args()
    if args.stage == "00": return stage00(args)
    require_host()
    if args.stage == "check": mounted_check(); return 0
    globals()["stage" + args.stage](args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_]+", str(exc)) else "PACKET_STAGE_FAILED"
        print(json.dumps({"verdict": "STOP", "error": code,
                          "instruction": "Preserve partial state; no automatic retry or rollback"}), file=sys.stderr)
        raise SystemExit(2)

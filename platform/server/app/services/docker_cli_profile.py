"""Resource policy for a Linux Docker CLI sandbox started by its trusted host."""

def container_flags():
    return [
        '--pull=never', '--network', 'none', '--user', '65534:65534', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true', '--init',
        '--cpus', '1', '--memory', '768m', '--memory-swap', '768m', '--pids-limit', '64',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,uid=65534,gid=65534,mode=0700',
    ]

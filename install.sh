#!/usr/bin/env bash
# Debian 12+ / Ubuntu 24.04+, systemd. All orders are simulated.
set -euo pipefail
umask 077
requested_mode=
cleanup_only=0
usage() {
  cat <<'HELP'
Usage: install.sh [--compare|--inventory|--qqq-hedge|--single|--cleanup|--help]
Debian 12+ / Ubuntu 24.04+, with systemd and Python 3.11+.
New installs run the 0.5% / 1% / 2% paper comparison by default.
Grid levels and paper margin budgets are unlimited, with 100x margin estimates.
The comparison includes a localhost dashboard on port 9876, accessed over SSH.
Repeating the command upgrades code and preserves mode, settings and data.
Earlier default experiments migrate to +/-30% with a backup and new ledgers.
Earlier seven-day centers migrate to three days with separate preserved ledgers.
Deployments use a cached offline preflight; full regression tests run in CI.
Unchanged dependencies, validated code and running services are reused.
  --compare  Start the three-grid comparison (also switches existing installs).
  --inventory  Start the five-scenario inventory comparison with its own ledgers.
  --qqq-hedge  Start the three-scenario Lighter QQQ / Variational US100 paper comparison.
  --single   Start one grid using config.json.
  --cleanup  Reclaim obsolete deployments without downloading or restarting.
  --help     Show this help without installing anything.
CL/BZ modes ask for vr-token with hidden input; QQQ hedge uses public market data.
No wallet key is needed.
HELP
}
if [[ $# -gt 1 ]]; then
  usage >&2; exit 1
fi
case ${1:-} in
  --compare) requested_mode=compare ;;
  --inventory) requested_mode=inventory ;;
  --qqq-hedge) requested_mode=qqq-hedge ;;
  --single) requested_mode=run ;;
  --cleanup) cleanup_only=1 ;;
  --help|-h) usage; exit 0 ;;
  '') ;;
  *) usage >&2; exit 1 ;;
esac

# Kept inside the downloaded installer so cleanup runs before fetching any code.
# This helper uses only Python's standard library and never imports application code.
storage() {
  python3 - "$app" "$conf" "$state" "$installer_source" "$@" <<'STORAGE_PY'
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


class DeploymentStorage:
    def __init__(self, app, conf, state, installer_source=''):
        self.app, self.conf, self.state = map(Path, (app, conf, state))
        self.releases = self.app / 'releases'
        self.source = self.app / 'source'
        self.current = (self.app / 'current').resolve()
        self.roots = []
        self.running = []
        for path in (self.conf, self.state, self.source):
            self.protect(path)
        if installer_source:
            self.protect(installer_source)
        # Preserve any configured path, including symlinks to a release. Config is
        # parsed as data; neither credentials nor configuration values are printed.
        for name in ('config.json', 'inventory-base.json', 'experiments.json', 'inventory.json', 'qqq-hedge.json'):
            path = self.conf / name
            self.protect(path)
            if not path.exists():
                continue
            spec = json.loads(path.read_text())
            if not isinstance(spec, dict):
                raise ValueError('Expected configuration object')
            for key in ('session_file', 'state_file', 'output_dir', 'base_config'):
                value = spec.get(key)
                if isinstance(value, str) and value:
                    configured = Path(value)
                    if not configured.is_absolute():
                        # Application paths are relative to the configuration file.
                        # Also retain the historical current-relative protection.
                        self.protect(path.parent / configured)
                        configured = self.current / configured
                    self.protect(configured)

    def protect(self, path):
        path = Path(path).absolute()
        self.roots.extend((path, path.resolve()))

    def inspect_services(self):
        for service in ('variational-grid.service', 'variational-grid-web.service'):
            result = subprocess.run(['systemctl', 'show', '--property=MainPID',
                                     '--property=LoadState', service], text=True, capture_output=True)
            props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if props.get('LoadState') == 'not-found':
                continue
            if result.returncode or props.get('LoadState') != 'loaded' or not props.get('MainPID', '').isdigit():
                raise RuntimeError('Cannot inspect service process; preserving all deployments.')
            pid = int(props['MainPID'])
            if pid:
                try:
                    self.running.append((Path('/proc') / str(pid) / 'cwd').resolve(strict=True))
                except OSError as error:
                    raise RuntimeError('Cannot inspect running directory; preserving all deployments.') from error

    @staticmethod
    def overlaps(left, right):
        return left == right or left in right.parents or right in left.parents

    def protected(self, path):
        path = Path(path)
        return (any(self.overlaps(path, root) for root in self.roots)
                or any(path == item or path in item.parents for item in
                       (self.current, Path.cwd().resolve(), *self.running)))

    def managed(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
            return False
        if path.parent == self.app and re.fullmatch(r'\.deploy\.[A-Za-z0-9]{6}', path.name):
            return self.marker(path, '.install-owned')
        if path.parent != self.releases or self.releases.is_symlink():
            return False
        if re.fullmatch(r'\.staging\.[A-Za-z0-9]{6}', path.name):
            return self.marker(path, '.install-owned')
        if not re.fullmatch(r'[0-9a-f]{40}', path.name):
            return False
        if self.marker(path, '.install-owned'):
            return True
        # Compatibility with releases made by the original installer. Unknown
        # directories, even those named like hashes, must not be removed.
        try:
            return (not (path / 'pyproject.toml').is_symlink()
                    and tomllib.loads((path / 'pyproject.toml').read_text())['project']['name'] == 'variational-cl-bz-grid'
                    and (path / 'install.sh').is_file()
                    and (path / 'variational_grid/__init__.py').is_file())
        except (OSError, ValueError, KeyError):
            return False

    @staticmethod
    def marker(path, name):
        marker = path / name
        return marker.is_file() and not marker.is_symlink()

    @staticmethod
    def contains_mount(path):
        # Do not descend into a mounted volume, including a same-device bind
        # mount. os.path.ismount alone does not identify all Linux bind mounts.
        if sys.platform == 'linux':
            mounts = Path('/proc/self/mountinfo').read_text().splitlines()
            for row in mounts:
                mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), row.split()[4]))
                if mount == path or path in mount.parents:
                    return True
        for root, directories, _ in os.walk(path, followlinks=False):
            if os.path.ismount(root) or any(os.path.ismount(Path(root) / item) for item in directories):
                return True
        return os.path.ismount(path)

    def remove(self, path):
        if not self.managed(path) or self.protected(path) or self.contains_mount(path):
            return False
        shutil.rmtree(path)
        return True

    def validation_key(self, release):
        marker = release / '.install-validation'
        if self.marker(release, '.install-validation'):
            key = marker.read_text().strip()
            if re.fullmatch(r'[0-9a-f]{64}', key):
                return key
        # Old installers stored only global stamps. Reconstruct the key for each
        # retained legacy release so a no-op upgrade still reuses validation.
        result = subprocess.run(['git', '-C', str(self.source), 'ls-tree', '-r', release.name, '--',
                                 'variational_grid', 'tests', 'install.sh', 'config.example.json',
                                 'experiments.example.json', 'inventory.example.json', 'qqq-hedge.example.json',
                                 'pyproject.toml'], capture_output=True)
        if result.returncode:
            return None
        version = subprocess.check_output([sys.executable, '--version'])
        return hashlib.sha256(version + result.stdout).hexdigest()

    def prune_validation(self):
        cache = self.app / 'validated'
        if (cache.is_symlink() or not cache.is_dir() or cache.resolve() != cache.absolute()
                or self.protected(cache) or self.contains_mount(cache)):
            return
        keys = set()
        for release in self.releases.iterdir():
            if self.managed(release) and re.fullmatch(r'[0-9a-f]{40}', release.name):
                key = self.validation_key(release)
                if key is None:
                    return  # Cannot establish references: retain the small stamps.
                keys.add(key)
        for stamp in cache.iterdir():
            if (stamp.is_file() and not stamp.is_symlink()
                    and re.fullmatch(r'[0-9a-f]{64}', stamp.name) and stamp.name not in keys
                    and not self.protected(stamp) and not os.path.ismount(stamp)):
                stamp.unlink()

    def prune(self, preferred=''):
        if not self.releases.is_dir() or self.releases.is_symlink():
            return
        candidates = [path for path in [*self.releases.iterdir(), *self.app.glob('.deploy.*')]
                      if self.managed(path)]
        ready = [path for path in candidates if path != self.current and
                 (self.marker(path, '.install-ready') or
                  (re.fullmatch(r'[0-9a-f]{40}', path.name) and not self.marker(path, '.install-owned')))]
        # Legacy releases predate success markers; conservatively retain the
        # newest validated archive as well as every actual running directory.
        backup = Path(preferred) if preferred and Path(preferred) in ready else None
        if backup is None and ready:
            backup = max(ready, key=lambda p: (p / '.install-ready').stat().st_mtime_ns
                         if self.marker(p, '.install-ready') else p.stat().st_mtime_ns)
        removed = sum(self.remove(path) for path in candidates if path != backup)
        self.prune_validation()
        print(f'Storage: reclaimed {removed} obsolete deployment(s); current, rollback and protected paths retained.')


def main():
    manager = DeploymentStorage(*sys.argv[1:5])
    manager.inspect_services()
    action, *args = sys.argv[5:]
    if action == 'prune':
        manager.prune(*args)
    elif action == 'abandon':
        for value in args:
            if value:
                manager.remove(Path(value))
        if manager.releases.is_dir():
            manager.prune_validation()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        # Do not expose preserved configuration values in parser error messages.
        print(f'Deployment storage inspection failed ({type(error).__name__}); no unsafe cleanup attempted.', file=sys.stderr)
        sys.exit(1)
STORAGE_PY
}

require_space() {
  local path=$1 required_kb=$2 required_inodes=$3 available inodes
  while [[ ! -e $path && $path != / ]]; do path=${path%/*}; [[ -n $path ]] || path=/; done
  available=$(df -Pk -- "$path" | awk 'END {print $4}')
  inodes=$(df -Pi -- "$path" | awk 'END {print $4}')
  [[ $available =~ ^[0-9]+$ && ( $inodes =~ ^[0-9]+$ || $inodes == - ) ]] || { echo 'Cannot determine available storage.' >&2; exit 1; }
  (( available >= required_kb )) || { printf 'Insufficient disk space: %s has %s MiB; this stage needs %s MiB. Services have not been switched.\n' "$path" "$((available / 1024))" "$((required_kb / 1024))" >&2; exit 1; }
  [[ $inodes == - ]] || (( inodes >= required_inodes )) || { printf 'Insufficient inodes: %s has %s; this stage needs %s. Services have not been switched.\n' "$path" "$inodes" "$required_inodes" >&2; exit 1; }
}

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo bash (root is needed to install the service).' >&2
  exit 1
fi
if [[ $(uname -s) != Linux ]] || ! command -v systemctl >/dev/null; then
  echo 'This installer requires Linux with systemd.' >&2
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo 'Supported: Debian 12+ / Ubuntu 24.04+ with apt.' >&2
  exit 1
fi

app=/opt/variational-grid
conf=/etc/variational-grid
state=/var/lib/variational-grid
repository=https://github.com/hxx344/variational-cl-bz-grid.git
account=variational-grid
installer_source=''
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
  installer_source=$(readlink -f -- "${BASH_SOURCE[0]}")
fi
staging= deployment= new_release= old_current=
if (( cleanup_only )) && [[ ! -e $app ]]; then
  echo 'No deployment to clean.'; exit 0
fi
[[ ! -L $app && ! -L $app/releases && ! -L $app/validated && ! -L $app/install.lock ]] || { echo 'Deployment directories must not be symlinks.' >&2; exit 1; }
[[ ! -e $app/current || -L $app/current ]] || { echo 'current must be an installer-managed symlink.' >&2; exit 1; }
install -d -m 755 "$app"
exec 9>"$app/install.lock"
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
old_current=$(readlink -f "$app/current" 2>/dev/null || true)
cleanup() {
  local status=$? candidate=''
  trap - EXIT INT TERM
  [[ $status == 0 ]] || candidate=$new_release
  if [[ -n $staging || -n $deployment || -n $candidate ]]; then
    # Refresh both process directories before deleting a failed candidate.
    # Failed inspection retains it; configuration and active code take priority.
    storage abandon "$staging" "$deployment" "$candidate" || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if command -v python3 >/dev/null; then
  storage prune
elif (( cleanup_only )); then
  echo 'Cleanup requires the Python 3.11+ used by the existing installation.' >&2; exit 1
fi
if (( cleanup_only )); then
  storage_paths=("$app")
  for path in "$conf" "$state"; do [[ ! -e $path ]] || storage_paths+=("$path"); done
  df -h "${storage_paths[@]}"
  df -i "${storage_paths[@]}"
  echo 'Cleanup complete; configuration, data and services unchanged.'
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
export PYTHONDONTWRITEBYTECODE=1
missing_packages=()
for package in python3 git ca-certificates; do
  if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
    missing_packages+=("$package")
  fi
done
if (( ${#missing_packages[@]} )); then
  require_space /var 524288 10000
  apt-get update -qq
  apt-get install -y -qq "${missing_packages[@]}"
else
  echo 'Dependencies present; skipping apt update/install.'
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required (Debian 12+ / Ubuntu 24.04+)"'

mode=${requested_mode:-$(cat "$conf/mode" 2>/dev/null || echo compare)}
[[ $mode == run || $mode == compare || $mode == inventory || $mode == qqq-hedge ]] || { echo 'Invalid saved service mode.' >&2; exit 1; }
experiment_name=experiments.json
config_name=config.json
if [[ $mode == inventory ]]; then
  experiment_name=inventory.json
  config_name=inventory-base.json
elif [[ $mode == qqq-hedge ]]; then
  experiment_name=qqq-hedge.json
fi

id "$account" >/dev/null 2>&1 || useradd --system --home-dir "$state" --shell /usr/sbin/nologin "$account"
install -d -m 755 "$app" "$app/releases" "$conf"
install -d -m 700 -o "$account" -g "$account" "$state"
require_space "$app" 8192 128
if [[ ! -d "$app/source/.git" ]]; then
  require_space "$app" 196608 8192
  git clone --depth 1 --branch main "$repository" "$app/source"
  revision=$(git -C "$app/source" rev-parse 'origin/main^{commit}')
else
  [[ $(git -C "$app/source" remote get-url origin) == "$repository" ]] || { echo 'Unexpected existing source remote.' >&2; exit 1; }
  revision=$(git -C "$app/source" ls-remote --exit-code origin refs/heads/main | cut -f1)
  [[ $revision =~ ^[a-f0-9]{40}$ ]] || { echo 'Cannot determine the remote main revision.' >&2; exit 1; }
  if ! git -C "$app/source" cat-file -e "$revision^{commit}" 2>/dev/null; then
    require_space "$app" 131072 4096
    git -C "$app/source" fetch --depth 1 origin "$revision"
  else
    echo 'Requested Git objects cached; skipping fetch.'
  fi
fi
[[ $revision =~ ^[a-f0-9]{40}$ ]] || exit 1
release="$app/releases/$revision"
# Version the quick check separately from the legacy full-test cache.
# Docs/tests-only revisions reuse it; failed checks never write a success stamp.
validation_key=$({
  printf '%s\n' 'quick-preflight-v1'
  python3 -c 'import sys, sqlite3; print(sys.version, sys.implementation.name, sys.implementation.cache_tag, sys.executable, sqlite3.sqlite_version)'
  git -C "$app/source" ls-tree -r "$revision" -- variational_grid deploy_check.py install.sh config.example.json experiments.example.json inventory.example.json qqq-hedge.example.json pyproject.toml
} | sha256sum | cut -d ' ' -f1)
install -d -m 755 "$app/validated"
validate_release() {
  if [[ $(cat "$app/validated/$validation_key" 2>/dev/null || true) == "$validation_key" ]]; then
    echo 'Matching release/runtime already checked; skipping quick preflight. Full tests run in CI.'
  else
    (cd "$1" && python3 -B deploy_check.py)
    printf '%s\n' "$validation_key" >"$app/validated/$validation_key"
  fi
  printf '%s\n' "$validation_key" >"$1/.install-validation"
}
if [[ ! -d "$release" ]]; then
  require_space "$app" 131072 4096
  staging=$(mktemp -d "$app/releases/.staging.XXXXXX")
  printf 'variational-grid\n' >"$staging/.install-owned"
  git -C "$app/source" archive "$revision" | tar -x -C "$staging"
  validate_release "$staging"
  chmod -R u=rwX,go=rX "$staging"
  mv -- "$staging" "$release"
  new_release=$release
  staging=
else
  validate_release "$release"
fi
require_space "$conf" 16384 512
require_space "$state" 16384 512
if [[ ! -f "$conf/config.json" ]]; then
  python3 - "$release/config.example.json" "$conf/config.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['session_file'] = '/var/lib/variational-grid/session.json'
data['state_file'] = '/var/lib/variational-grid/paper-unbounded-grid-100x.sqlite3'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/config.json"
fi
if [[ $mode == compare && ! -f "$conf/experiments.json" ]]; then
  python3 - "$release/experiments.example.json" "$conf/experiments.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/config.json'
data['output_dir'] = '/var/lib/variational-grid/comparison-pct-05-1-2-center3d-unbounded-grid-100x'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/experiments.json"
fi
if [[ $mode == inventory && ! -f "$conf/inventory-base.json" ]]; then
  # Freeze the starting economics independently of later legacy-mode migrations.
  install -m 644 "$conf/config.json" "$conf/inventory-base.json"
fi
if [[ $mode == inventory && ! -f "$conf/inventory.json" ]]; then
  python3 - "$release/inventory.example.json" "$conf/inventory.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/inventory-base.json'
data['output_dir'] = '/var/lib/variational-grid/inventory-pct-0-5-10-20-v1'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/inventory.json"
fi
if [[ $mode == qqq-hedge && ! -f "$conf/qqq-hedge.json" ]]; then
  python3 - "$release/qqq-hedge.example.json" "$conf/qqq-hedge.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/config.json'
data['output_dir'] = '/var/lib/variational-grid/qqq-hedge-scalper-v1'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/qqq-hedge.json"
fi
# Validate preserved config before switching the running version.
(cd "$release" && python3 - "$mode" <<'PY'
import json, sys
from dataclasses import replace
from pathlib import Path
from variational_grid.cli import configuration
from variational_grid.comparison import Experiment
base_path = Path('/etc/variational-grid') / ('inventory-base.json' if sys.argv[1] == 'inventory' else 'config.json')
config = configuration(base_path)
if sys.argv[1] in ('compare', 'inventory', 'qqq-hedge'):
    name = {'compare': 'experiments.json', 'inventory': 'inventory.json', 'qqq-hedge': 'qqq-hedge.json'}[sys.argv[1]]
    path = Path('/etc/variational-grid') / name
    spec = json.loads(path.read_text())
    if sys.argv[1] == 'inventory' and spec.get('kind') != 'inventory':
        raise SystemExit('Inventory service requires kind=inventory')
    if sys.argv[1] == 'qqq-hedge' and spec.get('kind') != 'qqq_hedge':
        raise SystemExit('QQQ hedge service requires kind=qqq_hedge')
    experiment = Experiment.load(path)
    expected = replace(config, center_hours=72, max_levels=None, max_margin_fraction=None) if sys.argv[1] == 'inventory' else config
    if sys.argv[1] == 'qqq-hedge':
        # Keep the schema's base reference stable; public QQQ/US100 feeds do not
        # use the legacy economics, session or single-strategy state file.
        valid_base = (path.parent / spec['base_config']).resolve() == base_path.resolve()
    else:
        valid_base = experiment.base == expected
    if not valid_base:
        raise SystemExit(f'Service experiments must use {base_path}')
    output = experiment.output
    if output == Path('/var/lib/variational-grid') or not output.is_relative_to('/var/lib/variational-grid'):
        raise SystemExit('Service experiment output must stay inside /var/lib/variational-grid')
root = Path('/var/lib/variational-grid').resolve()
for value in (() if sys.argv[1] == 'qqq-hedge' else (config.session_file, config.state_file)):
    path = Path(value).resolve()
    if path == root or not path.is_relative_to(root):
        raise SystemExit('Service session_file and state_file must stay inside /var/lib/variational-grid')
PY
)
if [[ $mode == qqq-hedge ]]; then
  (cd "$release" && python3 - <<'PY'
from variational_grid.qqq_migration import upgrade_qqq_defaults
backup = upgrade_qqq_defaults('/etc/variational-grid/qqq-hedge.json')
if backup:
    print(f'Updated to sequential QQQ scalper: three spacing/TP settings, dynamic 450s base wait, 3000 USDC hedge threshold; old configuration: {backup}; old ledgers preserved.')
else:
    print('QQQ settings unchanged; skipping migration.')
PY
  )
  echo 'QQQ / US100 public paper pricing selected; persisted shared reference quotes are enabled by default; no login session is required.'
elif ! (cd "$release" && runuser -u "$account" -- python3 -m variational_grid check-session --config "$conf/$config_name"); then
  echo 'A valid login session is needed for quantity-specific indicative quotes; public candles and statistics do not require one.'
  echo 'Paste only the vr-token cookie when prompted (input is hidden). No wallet private key is needed.'
  # /dev/tty keeps this interactive even when the installer arrives through curl | bash.
  (cd "$release" && runuser -u "$account" -- python3 -m variational_grid init-session --config "$conf/$config_name" </dev/tty)
fi
# Validate configuration and, for modes using it, the session above. Neither
# requires a restart when unchanged; session files are reloaded by those modes.
if [[ $mode == compare ]]; then
  (cd "$release" && python3 - "$conf/experiments.json" <<'PY'
import sys
from variational_grid.migration import upgrade_experiment
backup = upgrade_experiment(sys.argv[1])
if backup:
    print(f'Updated grid steps to 0.5% / 1% / 2%, each direction 30% (60/30/15 levels); old settings: {backup}; old ledgers preserved.')
else:
    print('Experiment settings unchanged; skipping migration.')
PY
  )
fi
if [[ $mode == run || $mode == compare ]]; then
  (cd "$release" && python3 - "$mode" "$conf" <<'PY'
import sys
from pathlib import Path
from variational_grid.migration import upgrade_center, upgrade_margin_limit, upgrade_unbounded_grid
comparison = sys.argv[1] == 'compare'
path = Path(sys.argv[2]) / ('experiments.json' if comparison else 'config.json')
backup = upgrade_center(path, comparison=comparison)
if backup:
    print(f'Updated center to 3 days (72 closed hours); a new simulation will start. Old settings: {backup}; old ledgers preserved.')
else:
    print('Three-day center already configured; skipping center migration.')
backup = upgrade_margin_limit(path, comparison=comparison)
if backup:
    print(f'Removed paper position/margin budget; grid levels and drawdown rules preserved. A new simulation will start. Old settings: {backup}; old ledgers preserved.')
else:
    print('Position budget settings unchanged; skipping margin migration.')
backup = upgrade_unbounded_grid(path, comparison=comparison)
if backup:
    print(f'Updated to unlimited grid levels and 100x paper leverage; position/margin budgets disabled. Old settings: {backup}; old ledgers preserved in a separate run.')
else:
    print('Grid/leverage settings unchanged; skipping unbounded-grid migration.')
PY
  )
else
  printf '%s strategy selected; skipping legacy grid migrations.\n' "$mode"
fi
settings_key=$({
  printf '%s\n' "$mode"
  if [[ $mode != qqq-hedge ]]; then
    # Public QQQ/US100 data uses none of the legacy base economics or credentials.
    sha256sum "$conf/$config_name"
  fi
  if [[ $mode != run ]]; then sha256sum "$conf/$experiment_name"; fi
} | sha256sum | cut -d ' ' -f1)
engine_key=$({
  printf '%s\n' "$settings_key"
  python3 --version
  git -C "$app/source" ls-tree -r "$revision" -- variational_grid | sed '\|[[:space:]]variational_grid/web/|d; \|[[:space:]]variational_grid/dashboard.py$|d'
} | sha256sum | cut -d ' ' -f1)
web_key=$({
  printf '%s\n' "$settings_key"
  python3 --version
  git -C "$app/source" ls-tree -r "$revision" -- variational_grid
} | sha256sum | cut -d ' ' -f1)
deployment=$(mktemp -d "$app/.deploy.XXXXXX")
printf 'variational-grid\n' >"$deployment/.install-owned"
cat >"$deployment/variational-grid.service" <<'UNIT'
[Unit]
Description=Variational CL BZ paper spread grid
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=variational-grid
Group=variational-grid
WorkingDirectory=/opt/variational-grid/current
ExecStart=/usr/bin/python3 -m variational_grid run --config /etc/variational-grid/config.json
Restart=on-failure
RestartSec=30
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/variational-grid
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
UNIT
if [[ $mode != run ]]; then
  sed -i "s|^ExecStart=.*|ExecStart=/usr/bin/python3 -m variational_grid compare --experiments $conf/$experiment_name|" "$deployment/variational-grid.service"
  cat >"$deployment/variational-grid-web.service" <<UNIT
[Unit]
Description=Variational paper grid dashboard (localhost)
After=variational-grid.service

[Service]
Type=simple
User=variational-grid
Group=variational-grid
WorkingDirectory=/opt/variational-grid/current
ExecStart=/usr/bin/python3 -m variational_grid dashboard --experiments $conf/$experiment_name --port 9876
Restart=on-failure
RestartSec=10
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
# SQLite read-only connections may need to maintain shared-memory sidecars.
ReadWritePaths=/var/lib/variational-grid
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
UNIT
elif [[ -f /etc/systemd/system/variational-grid-web.service ]]; then
  if systemctl is-active --quiet variational-grid-web.service || systemctl is-enabled --quiet variational-grid-web.service; then
    systemctl disable --now variational-grid-web.service
  fi
fi
if [[ $(readlink -f "$app/current" 2>/dev/null || true) != "$release" ]]; then
  ln -sfn "$release" "$app/current"
fi
if [[ $(cat "$conf/mode" 2>/dev/null || true) != "$mode" ]]; then
  printf '%s\n' "$mode" >"$conf/mode"
  chmod 644 "$conf/mode"
fi
units_changed=false
engine_unit_changed=false
web_unit_changed=false
for service in variational-grid.service variational-grid-web.service; do
  if [[ -f "$deployment/$service" ]] && ! cmp -s "$deployment/$service" "/etc/systemd/system/$service"; then
    # Invalidate before writing so an interrupted reload/restart is retried.
    : >"$app/applied-units"
    if [[ $service == variational-grid.service ]]; then : >"$app/applied-engine"; else : >"$app/applied-web"; fi
    install -m 644 "$deployment/$service" "/etc/systemd/system/$service"
    units_changed=true
    if [[ $service == variational-grid.service ]]; then engine_unit_changed=true; else web_unit_changed=true; fi
  fi
done
units_key=$({
  sha256sum /etc/systemd/system/variational-grid.service
  if [[ -f /etc/systemd/system/variational-grid-web.service ]]; then sha256sum /etc/systemd/system/variational-grid-web.service; fi
} | sha256sum | cut -d ' ' -f1)
if $units_changed || [[ $(cat "$app/applied-units" 2>/dev/null || true) != "$units_key" ]]; then
  systemctl daemon-reload
  printf '%s\n' "$units_key" >"$app/applied-units"
fi
apply_service() {
  local service=$1 key=$2 unit_changed=$3 stamp=$4
  if ! systemctl is-enabled --quiet "$service"; then systemctl enable "$service"; fi
  if [[ $(cat "$stamp" 2>/dev/null || true) != "$key" ]] || $unit_changed || ! systemctl is-active --quiet "$service"; then
    systemctl restart "$service"
    systemctl --no-pager --full status "$service"
    printf '%s\n' "$key" >"$stamp"
  else
    printf '%s unchanged and running; skipping restart.\n' "$service"
  fi
}
apply_service variational-grid.service "$engine_key" "$engine_unit_changed" "$app/applied-engine"
if [[ $mode != run ]]; then
  apply_service variational-grid-web.service "$web_key" "$web_unit_changed" "$app/applied-web"
fi
touch "$release/.install-ready"
storage prune "$old_current"

echo 'Paper simulation ready. Settings and ledger are preserved on repeat installation.'
printf 'Service mode: %s\n' "$mode"
printf 'Settings: %s/%s\n' "$conf" "$config_name"
if [[ $mode != run ]]; then
  printf 'Experiments: %s/%s\n' "$conf" "$experiment_name"
  printf 'Report: <output_dir from %s>/public/index.html\n' "$experiment_name"
  echo 'Dashboard: run this on your own computer (keep the terminal open):'
  echo '  ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -L 127.0.0.1:18765:127.0.0.1:9876 USER@SERVER_IP'
  echo 'Then open http://127.0.0.1:18765/ in your browser. No public web port is required.'
  echo 'Keep using an existing tunnel after upgrade; do not open another on the same local port.'
  echo 'Windows reconnect helper: https://github.com/hxx344/variational-cl-bz-grid#ssh-tunnel-recovery'
  echo 'A dashboard restart may briefly interrupt HTTP; an SSH Connection reset requires reconnecting the SSH transport.'
  echo 'Dashboard logs: journalctl -u variational-grid-web -f'
fi
echo 'Logs: journalctl -u variational-grid -f'
echo 'Stop: sudo systemctl stop variational-grid'
echo 'Restart: sudo systemctl restart variational-grid'

#!/usr/bin/env bash
# Debian 12+ / Ubuntu 24.04+, systemd. All orders are simulated.
set -euo pipefail
umask 077
requested_mode=
usage() {
  cat <<'HELP'
Usage: install.sh [--compare|--single|--help]
Debian 12+ / Ubuntu 24.04+, with systemd and Python 3.11+.
New installs run the 0.15 / 0.20 / 0.25 paper comparison by default.
The comparison includes a localhost dashboard on port 9876, accessed over SSH.
Repeating the command upgrades code and preserves mode, settings and data.
Unchanged dependencies, validated code and running services are reused.
  --compare  Start the three-grid comparison (also switches existing installs).
  --single   Start one grid using config.json.
  --help     Show this help without installing anything.
The first install asks for vr-token with hidden input; no wallet key is needed.
HELP
}
if [[ $# -gt 1 ]]; then
  usage >&2; exit 1
fi
case ${1:-} in
  --compare) requested_mode=compare ;;
  --single) requested_mode=run ;;
  --help|-h) usage; exit 0 ;;
  '') ;;
  *) usage >&2; exit 1 ;;
esac

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

export DEBIAN_FRONTEND=noninteractive
export PYTHONDONTWRITEBYTECODE=1
missing_packages=()
for package in python3 git ca-certificates; do
  if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
    missing_packages+=("$package")
  fi
done
if (( ${#missing_packages[@]} )); then
  apt-get update -qq
  apt-get install -y -qq "${missing_packages[@]}"
else
  echo 'Dependencies present; skipping apt update/install.'
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required (Debian 12+ / Ubuntu 24.04+)"'

app=/opt/variational-grid
conf=/etc/variational-grid
state=/var/lib/variational-grid
repository=https://github.com/hxx344/variational-cl-bz-grid.git
account=variational-grid
mode=${requested_mode:-$(cat "$conf/mode" 2>/dev/null || echo compare)}
[[ $mode == run || $mode == compare ]] || { echo 'Invalid saved service mode.' >&2; exit 1; }

id "$account" >/dev/null 2>&1 || useradd --system --home-dir "$state" --shell /usr/sbin/nologin "$account"
install -d -m 755 "$app" "$app/releases" "$conf"
install -d -m 700 -o "$account" -g "$account" "$state"
if [[ ! -d "$app/source/.git" ]]; then
  git clone --depth 1 --branch main "$repository" "$app/source"
  revision=$(git -C "$app/source" rev-parse 'origin/main^{commit}')
else
  [[ $(git -C "$app/source" remote get-url origin) == "$repository" ]] || { echo 'Unexpected existing source remote.' >&2; exit 1; }
  revision=$(git -C "$app/source" ls-remote --exit-code origin refs/heads/main | cut -f1)
  [[ $revision =~ ^[a-f0-9]{40}$ ]] || { echo 'Cannot determine the remote main revision.' >&2; exit 1; }
  if ! git -C "$app/source" cat-file -e "$revision^{commit}" 2>/dev/null; then
    git -C "$app/source" fetch --depth 1 origin "$revision"
  else
    echo 'Requested Git objects cached; skipping fetch.'
  fi
fi
[[ $revision =~ ^[a-f0-9]{40}$ ]] || exit 1
release="$app/releases/$revision"
staging=
deployment=
cleanup() {
  if [[ -n $staging && $staging == "$app/releases/.staging."* && -d $staging ]]; then
    rm -rf -- "$staging"
  fi
  if [[ -n $deployment && $deployment == "$app/.deploy."* && -d $deployment ]]; then
    rm -rf -- "$deployment"
  fi
}
trap cleanup EXIT
# Tests depend on their source, deployment logic, examples and Python runtime.
# Docs-only revisions can reuse a successful result; failed runs never write it.
validation_key=$({
  python3 --version
  git -C "$app/source" ls-tree -r "$revision" -- variational_grid tests install.sh config.example.json experiments.example.json pyproject.toml
} | sha256sum | cut -d ' ' -f1)
install -d -m 755 "$app/validated"
validate_release() {
  if [[ $(cat "$app/validated/$validation_key" 2>/dev/null || true) == "$validation_key" ]]; then
    echo 'Matching code/tests/Python already validated; skipping full test suite.'
  else
    (cd "$1" && python3 -m unittest discover -s tests -v)
    printf '%s\n' "$validation_key" >"$app/validated/$validation_key"
  fi
}
if [[ ! -d "$release" ]]; then
  staging=$(mktemp -d "$app/releases/.staging.XXXXXX")
  git -C "$app/source" archive "$revision" | tar -x -C "$staging"
  validate_release "$staging"
  chmod -R u=rwX,go=rX "$staging"
  mv -- "$staging" "$release"
  staging=
else
  validate_release "$release"
fi
if [[ ! -f "$conf/config.json" ]]; then
  python3 - "$release/config.example.json" "$conf/config.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['session_file'] = '/var/lib/variational-grid/session.json'
data['state_file'] = '/var/lib/variational-grid/paper.sqlite3'
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
data['output_dir'] = '/var/lib/variational-grid/comparison-015-020-025'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/experiments.json"
fi
# Validate preserved config before switching the running version.
(cd "$release" && python3 - "$mode" <<'PY'
import sys
from pathlib import Path
from variational_grid.cli import configuration
from variational_grid.comparison import Experiment
config = configuration('/etc/variational-grid/config.json')
if sys.argv[1] == 'compare':
    experiment = Experiment.load('/etc/variational-grid/experiments.json')
    if experiment.base != config:
        raise SystemExit('Service experiments must use /etc/variational-grid/config.json')
    output = experiment.output
    if output == Path('/var/lib/variational-grid') or not output.is_relative_to('/var/lib/variational-grid'):
        raise SystemExit('Service experiment output must stay inside /var/lib/variational-grid')
root = Path('/var/lib/variational-grid').resolve()
for value in (config.session_file, config.state_file):
    path = Path(value).resolve()
    if path == root or not path.is_relative_to(root):
        raise SystemExit('Service session_file and state_file must stay inside /var/lib/variational-grid')
PY
)
if ! (cd "$release" && runuser -u "$account" -- python3 -m variational_grid check-session --config "$conf/config.json"); then
  echo 'A valid login session is needed for quantity-specific indicative quotes; public candles and statistics do not require one.'
  echo 'Paste only the vr-token cookie when prompted (input is hidden). No wallet private key is needed.'
  # /dev/tty keeps this interactive even when the installer arrives through curl | bash.
  (cd "$release" && runuser -u "$account" -- python3 -m variational_grid init-session --config "$conf/config.json" </dev/tty)
fi
# Always validate configuration and the session above. Neither requires a restart
# when unchanged; refreshed session files are already reloaded by the simulator.
settings_key=$({
  printf '%s\n' "$mode"
  sha256sum "$conf/config.json"
  if [[ $mode == compare ]]; then sha256sum "$conf/experiments.json"; fi
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
if [[ $mode == compare ]]; then
  sed -i 's|^ExecStart=.*|ExecStart=/usr/bin/python3 -m variational_grid compare --experiments /etc/variational-grid/experiments.json|' "$deployment/variational-grid.service"
  cat >"$deployment/variational-grid-web.service" <<'UNIT'
[Unit]
Description=Variational read-only grid dashboard (localhost)
After=variational-grid.service

[Service]
Type=simple
User=variational-grid
Group=variational-grid
WorkingDirectory=/opt/variational-grid/current
ExecStart=/usr/bin/python3 -m variational_grid dashboard --experiments /etc/variational-grid/experiments.json --port 9876
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
if [[ $mode == compare ]]; then
  apply_service variational-grid-web.service "$web_key" "$web_unit_changed" "$app/applied-web"
fi
echo 'Paper simulation ready. Settings and ledger are preserved on repeat installation.'
printf 'Service mode: %s\n' "$mode"
echo 'Settings: /etc/variational-grid/config.json'
if [[ $mode == compare ]]; then
  echo 'Experiments: /etc/variational-grid/experiments.json'
  echo 'Report: <output_dir from experiments.json>/public/index.html'
  echo 'Dashboard: run this on your own computer (keep the terminal open):'
  echo '  ssh -N -o ExitOnForwardFailure=yes -L 18765:127.0.0.1:9876 USER@SERVER_IP'
  echo 'Then open http://127.0.0.1:18765/ in your browser. No public web port is required.'
  echo 'Dashboard logs: journalctl -u variational-grid-web -f'
fi
echo 'Logs: journalctl -u variational-grid -f'
echo 'Stop: sudo systemctl stop variational-grid'
echo 'Restart: sudo systemctl restart variational-grid'

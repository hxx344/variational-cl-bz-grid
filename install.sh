#!/usr/bin/env bash
# Debian 12+ / Ubuntu 24.04+, systemd. All orders are simulated.
set -euo pipefail
umask 077

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
apt-get update -qq
apt-get install -y -qq python3 git ca-certificates
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required (Debian 12+ / Ubuntu 24.04+)"'

app=/opt/variational-grid
conf=/etc/variational-grid
state=/var/lib/variational-grid
repository=https://github.com/hxx344/variational-cl-bz-grid.git
account=variational-grid

id "$account" >/dev/null 2>&1 || useradd --system --home-dir "$state" --shell /usr/sbin/nologin "$account"
install -d -m 755 "$app" "$app/releases" "$conf"
install -d -m 700 -o "$account" -g "$account" "$state"
if [[ ! -d "$app/source/.git" ]]; then
  git clone --depth 1 --branch main "$repository" "$app/source"
else
  [[ $(git -C "$app/source" remote get-url origin) == "$repository" ]] || { echo 'Unexpected existing source remote.' >&2; exit 1; }
  git -C "$app/source" fetch --depth 1 origin main
fi
revision=$(git -C "$app/source" rev-parse 'origin/main^{commit}')
[[ $revision =~ ^[a-f0-9]{40}$ ]] || exit 1
release="$app/releases/$revision"
if [[ ! -d "$release" ]]; then
  install -d -m 755 "$release"
  git -C "$app/source" archive "$revision" | tar -x -C "$release"
  chmod -R u=rwX,go=rX "$release"
fi
(cd "$release" && python3 -m unittest discover -s tests -v)
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
# Validate preserved config before switching the running version.
(cd "$release" && python3 - <<'PY'
from pathlib import Path
from variational_grid.cli import configuration
config = configuration('/etc/variational-grid/config.json')
root = Path('/var/lib/variational-grid').resolve()
for value in (config.session_file, config.state_file):
    path = Path(value).resolve()
    if path == root or not path.is_relative_to(root):
        raise SystemExit('Service session_file and state_file must stay inside /var/lib/variational-grid')
PY
)
if ! (cd "$release" && runuser -u "$account" -- python3 -c 'from variational_grid.client import Client; from variational_grid.cli import configuration; Client(configuration("/etc/variational-grid/config.json").session_file).check_session()'); then
  echo 'Paste only the vr-token cookie when prompted (input is hidden). No wallet private key is needed.'
  # /dev/tty keeps this interactive even when the installer arrives through curl | bash.
  (cd "$release" && runuser -u "$account" -- python3 -m variational_grid init-session --config "$conf/config.json" </dev/tty)
fi
ln -sfn "$release" "$app/current"
cat >/etc/systemd/system/variational-grid.service <<'UNIT'
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
systemctl daemon-reload
systemctl enable variational-grid.service
systemctl restart variational-grid.service
systemctl --no-pager --full status variational-grid.service
echo 'Paper simulation started. Settings and ledger are preserved on repeat installation.'
echo 'Logs: journalctl -u variational-grid -f'

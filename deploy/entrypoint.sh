#!/bin/sh
set -eu
mkdir -p /MaiMBot/plugins /MaiMBot/data/wemaa
if [ ! -e /MaiMBot/plugins/website-agent ]; then
  cp -a /opt/wemaa/builtin-plugins/website-agent /MaiMBot/plugins/website-agent
fi
exec python bot.py "$@"

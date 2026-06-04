#!/usr/bin/env bash
# BTC 15m signal monitor — quality alerts only
# Fires on: new market open, conf crossing 40/60/80, direction flip
# Poll cadence: 8s early (>10min left), 15s mid (5-10min), 25s late (<5min)

API="http://localhost:9050/api/crypto/signal"

fetch() {
  curl -s --max-time 5 "$API" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('status') != 'ok':
    raise SystemExit(1)
m_dir   = d['direction']
m_conf  = round(d['confidence'], 1)
m_price = round((d['price'] or 0) * 100, 1)
m_spot  = round(d['spot'], 2)
m_strk  = round(d['floor_strike'], 2)
m_dist  = round(d['distance'], 2)
m_mins  = round(d['mins_left'], 1)
m_mom   = round(d.get('momentum') or 0, 1)
m_ypct  = round(d.get('yes_pct') or 0, 1)
m_wtrnd = round(d.get('whale_trend') or 0, 1)
m_tick  = d['ticker']
bucket = 0
if m_conf >= 80: bucket = 3
elif m_conf >= 60: bucket = 2
elif m_conf >= 40: bucket = 1
print(m_tick, m_dir, m_price, m_conf, bucket, m_spot, m_strk, m_dist, m_mins, m_mom, m_ypct, m_wtrnd)
" 2>/dev/null
}

# Seed state from current market so startup doesn't false-fire
read last_ticker last_dir _ _ last_bucket _ _ _ _ _ _ _ <<< $(fetch)
last_bucket=${last_bucket:-0}
last_dir=${last_dir:-""}

while true; do
  read ticker dir price conf bucket spot strike dist mins mom ypct wtrnd <<< $(fetch)

  if [ -z "$ticker" ]; then
    sleep 10
    continue
  fi

  if [ "$ticker" != "$last_ticker" ]; then
    # New market — always alert
    echo "NEW_MARKET|$ticker|$dir|$price|$conf|$bucket|$spot|$strike|$dist|$mins|$mom|$ypct|$wtrnd"
    last_ticker="$ticker"
    last_bucket="$bucket"
    last_dir="$dir"

  else
    fired=0

    # Direction flip — highest priority
    if [ "$dir" != "$last_dir" ] && [ -n "$last_dir" ]; then
      echo "DIR_FLIP|$ticker|$dir|$price|$conf|$bucket|$spot|$strike|$dist|$mins|$mom|$ypct|$wtrnd"
      last_dir="$dir"
      last_bucket="$bucket"
      fired=1
    fi

    # Conf threshold crossed (only if no dir flip this tick)
    if [ "$fired" -eq 0 ] && [ "$bucket" != "$last_bucket" ]; then
      if [ "$bucket" -gt "$last_bucket" ]; then
        event="CONF_UP"
      else
        event="CONF_DOWN"
      fi
      echo "$event|$ticker|$dir|$price|$conf|$bucket|$spot|$strike|$dist|$mins|$mom|$ypct|$wtrnd"
      last_bucket="$bucket"
    fi
  fi

  # Adaptive poll — frequent early (signal forming), slower later (signal locked)
  mins_int=${mins%.*}
  if (( mins_int >= 10 )); then
    sleep 8
  elif (( mins_int >= 5 )); then
    sleep 15
  else
    sleep 25
  fi
done

#!/usr/bin/env bash
# API サーバーをバックグラウンドで起動・停止する。
#
# 使い方:
#   scripts/server.sh start      # バックグラウンドで起動
#   scripts/server.sh stop       # 停止
#   scripts/server.sh restart    # 再起動（.env や persona.yaml を変えたとき）
#   scripts/server.sh status     # 動いているか確認
#   scripts/server.sh logs       # ログを流し続けて表示（Ctrl+C で抜ける。サーバーは止まらない）
#
# 環境変数（省略可）:
#   HOST  待ち受けるアドレス（既定: 127.0.0.1。手前に HTTPS の Web サーバーを置く場合もこのままでよい）
#   PORT  待ち受けるポート（既定: 8000）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
PID_FILE="$ROOT/run/server.pid"
LOG_FILE="$ROOT/logs/server.log"
UVICORN="$ROOT/.venv/bin/uvicorn"

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
  else
    rm -f "$PID_FILE" # プロセスが残っていない古い PID ファイル
    return 1
  fi
}

start() {
  if pid="$(running_pid)"; then
    echo "すでに起動しています (PID $pid, http://$HOST:$PORT)"
    return 0
  fi
  if [[ ! -x "$UVICORN" ]]; then
    echo "$UVICORN がありません。先に依存パッケージを入れてください:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    return 1
  fi
  mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

  cd "$ROOT"
  nohup "$UVICORN" app.server:app --host "$HOST" --port "$PORT" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"

  # persona.yaml の不備などで起動直後に落ちることがあるので、少し待って確認する
  sleep 2
  if pid="$(running_pid)"; then
    echo "起動しました (PID $pid, http://$HOST:$PORT)  ログ: $LOG_FILE"
  else
    echo "起動に失敗しました。ログの末尾:" >&2
    tail -n 20 "$LOG_FILE" >&2
    return 1
  fi
}

stop() {
  local pid
  if ! pid="$(running_pid)"; then
    echo "起動していません"
    return 0
  fi
  kill "$pid"
  # 通話中のストリームを閉じ終えるまで最大 10 秒待ち、それでも残っていれば強制終了する
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "終了しないため強制終了します (PID $pid)"
    kill -9 "$pid"
  fi
  rm -f "$PID_FILE"
  echo "停止しました"
}

status() {
  if pid="$(running_pid)"; then
    echo "起動中 (PID $pid, http://$HOST:$PORT)"
  else
    echo "停止中"
    return 3
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop && start ;;
  status) status ;;
  logs)
    mkdir -p "$(dirname "$LOG_FILE")" && touch "$LOG_FILE"
    tail -n 50 -f "$LOG_FILE"
    ;;
  *)
    echo "使い方: $0 {start|stop|restart|status|logs}" >&2
    exit 2
    ;;
esac

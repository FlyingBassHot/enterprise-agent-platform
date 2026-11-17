#!/usr/bin/env bash
# 轮询远端：别人 push 或开/合并 PR 时弹桌面通知，并自动 fetch（不动工作区）。
#
# 用法:
#   scripts/watch-remote.sh [仓库路径] [remote]
#
# 通常由 systemd user timer 每分钟调用一次；也可以手动执行。
# 状态保存在 ${XDG_STATE_HOME:-~/.local/state}/git-watch/。
# 可选: 在 ~/.config/git-watch.env 中设置 NTFY_TOPIC / NTFY_SERVER，
#       同时把通知推到手机。
set -euo pipefail

if [ $# -gt 0 ]; then
  repo=$1
else
  repo=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "watch-remote: 当前目录不是 git 仓库" >&2
    exit 1
  }
fi
remote=${2:-origin}

cd "$repo"

# 可选的外部配置（ntfy 推送）
if [ -f "$HOME/.config/git-watch.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$HOME/.config/git-watch.env"
  set +a
fi

state_dir=${XDG_STATE_HOME:-$HOME/.local/state}/git-watch
mkdir -p "$state_dir"

# 同一仓库只跑一个实例
exec 9>"$state_dir/lock"
flock -n 9 || exit 0

key=$(printf '%s\n%s\n' "$repo" "$remote" | sha1sum | cut -c1-12)
state_file=$state_dir/$key.refs
pr_file=$state_dir/$key.prs

notify() {
  local title=$1 body=$2
  notify-send -a git-watch -i git "$title" "$body" || true
  printf '%s\n%s\n' "$title" "$body"
  if [ -n "${NTFY_TOPIC:-}" ]; then
    jq -n --arg topic "$NTFY_TOPIC" --arg title "$title" --arg message "$body" \
      '{topic:$topic, title:$title, message:$message, tags:["git","arrow_up"], priority:3}' \
      | curl -fsS -m 10 -H 'Content-Type: application/json' --data-binary @- "${NTFY_SERVER:-https://ntfy.sh}" >/dev/null || true
  fi
}

github_slug() {
  local url
  [ -n "${GIT_WATCH_SLUG:-}" ] && {
    printf '%s' "$GIT_WATCH_SLUG"
    return 0
  }
  url=$(git remote get-url "$remote" 2>/dev/null) || return 0
  case $url in
    *github.com[:/]*) printf '%s' "$url" | sed -E 's#.*github\.com[:/]##; s#\.git$##' ;;
  esac
}

# 新 PR / PR 被合并或关闭时通知；评论等噪音忽略。
check_prs() {
  local slug=$1 json me="" line number state merged login title pr_url prev old_state
  local -a body=()
  local baseline=0

  [ -n "$slug" ] || return 0
  command -v gh >/dev/null 2>&1 || return 0

  json=$(timeout 30 gh api "repos/$slug/pulls?state=all&sort=updated&direction=desc&per_page=20" \
    --jq '.[] | [.number, .state, (.merged_at // "-"), .user.login, .title, .html_url] | @tsv' 2>/dev/null) || return 0

  [ -f "$pr_file" ] || baseline=1

  declare -A old=()
  if [ "$baseline" -eq 0 ]; then
    while IFS=$'\t' read -r number state merged; do
      [ -n "${number:-}" ] && old[$number]="$state|$merged"
    done < "$pr_file"
  fi

  local current=""
  while IFS=$'\t' read -r number state merged login title pr_url; do
    [ -z "${number:-}" ] && continue
    current+="$number"$'\t'"$state"$'\t'"$merged"$'\n'

    [ "$baseline" -eq 1 ] && continue

    prev=${old[$number]:-}
    if [ -z "$prev" ]; then
      if [ -z "$me" ]; then me=$(timeout 15 gh api user --jq .login 2>/dev/null || true); fi
      [ "$login" = "$me" ] && continue
      body+=("新 PR #$number: $title（by $login）"$'\n'"$pr_url")
      continue
    fi

    old_state=${prev%%|*}
    if [ "$old_state" = open ] && [ "$state" = closed ]; then
      if [ "$merged" != "-" ]; then
        body+=("PR #$number 已合并: $title"$'\n'"$pr_url")
      else
        body+=("PR #$number 已关闭: $title"$'\n'"$pr_url")
      fi
    fi
  done <<< "$json"

  printf '%s' "$current" > "$pr_file.tmp"
  mv "$pr_file.tmp" "$pr_file"

  if [ ${#body[@]} -gt 0 ]; then
    notify "$(basename "$repo"): PR 有更新" "$(printf '%s\n' "${body[@]}")"
  fi
}

if ! current_refs=$(git ls-remote --heads --quiet "$remote" 2>/dev/null); then
  echo "watch-remote: 无法访问 $remote，跳过本轮" >&2
  exit 0
fi
current_refs=$(printf '%s\n' "$current_refs" | grep . | sort -k2 || true)

write_state() {
  printf '%s\n' "$current_refs" > "$state_file.tmp"
  mv "$state_file.tmp" "$state_file"
}

# 首次运行只记录基线，不通知
if [ ! -f "$state_file" ]; then
  write_state
  echo "watch-remote: 已记录基线（$(printf '%s\n' "$current_refs" | grep -c . || true) 个分支）"
  check_prs "$(github_slug)"
  exit 0
fi

declare -A prev=()
while read -r sha ref; do
  [ -n "${ref:-}" ] && prev[$ref]=$sha
done < "$state_file"

changed_refs=()
while read -r sha ref; do
  [ -z "${ref:-}" ] && continue
  old=${prev[$ref]:-}
  if [ -z "$old" ]; then
    changed_refs+=("new:$ref:$sha")
  elif [ "$old" != "$sha" ]; then
    changed_refs+=("upd:$ref:$old:$sha")
  fi
done <<< "$current_refs"

if [ ${#changed_refs[@]} -eq 0 ]; then
  write_state
else
  # 只更新 remote-tracking refs，不动工作区
  git fetch --quiet --prune "$remote" || true

  self_email=$(git config user.email || true)
  body=""
  has_news=0

  for entry in "${changed_refs[@]}"; do
    IFS=: read -r kind ref old new <<< "$entry"
    branch=${ref#refs/heads/}

    if [ "$kind" = new ]; then
      tip_author=$(git log -1 --format='%ae' "$old" 2>/dev/null || true)
      if [ -n "$self_email" ] && [ "$tip_author" = "$self_email" ]; then
        continue
      fi
      body+="新分支 $branch @ ${old:0:7} — $(git log -1 --format='%s' "$old" 2>/dev/null || true)"$'\n'
      has_news=1
      continue
    fi

    if ! git cat-file -e "$old^{commit}" 2>/dev/null || ! git cat-file -e "$new^{commit}" 2>/dev/null; then
      body+="$branch: 远端被更新（本地缺少旧提交，可能是 force push）：${new:0:7}"$'\n'
      has_news=1
      continue
    fi

    count=$(git rev-list --count "$old..$new")
    [ "$count" -eq 0 ] && continue

    # 全是自己推的就不打扰
    authors=$(git log --format='%ae' "$old..$new" | sort -u)
    if [ -n "$self_email" ] && [ -z "$(printf '%s\n' "$authors" | grep -vxF "$self_email" || true)" ]; then
      continue
    fi

    has_news=1
    body+="$branch: $count 个新提交"$'\n'
    while IFS= read -r line; do
      body+="  $line"$'\n'
    done < <(git log --format='%h %s — %an' -n 5 "$old..$new")
    if [ "$count" -gt 5 ]; then
      body+="  … 还有 $((count - 5)) 个"$'\n'
    fi
  done

  write_state

  if [ "$has_news" -eq 1 ]; then
    notify "$(basename "$repo"): 远端有新的 push" "${body%$'\n'}"
  else
    echo "watch-remote: 只有自己的推送，跳过通知"
  fi
fi

check_prs "$(github_slug)"

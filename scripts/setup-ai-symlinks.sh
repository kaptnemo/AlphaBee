#!/bin/bash
# ============================================================
# AI 工具 Symlink 恢复脚本
# ============================================================
# 当 symlink 在 Windows 上失效（Git for Windows core.symlinks=false）
# 或任何环境下链接丢失时，用此脚本重建。
#
# Linux/macOS 上 git clone 后 symlink 通常自动生效，无需运行。
# ============================================================

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

create_link() {
  local src="$1"
  local dst="$2"

  if [ -L "$dst" ]; then
    echo "[OK] $dst → $(readlink "$dst")"
  elif [ -d "$dst" ]; then
    echo "[SKIP] $dst 是真实目录（可能有本地内容），跳过"
  elif [ -f "$dst" ]; then
    echo "[SKIP] $dst 是普通文件（可能是 Windows 降级副本），跳过"
  else
    ln -s "$src" "$dst"
    echo "[DONE] $dst → $src"
  fi
}

create_link "../.ai/skills"  "$ROOT/.claude/skills"
create_link "../.ai/skills"  "$ROOT/.github/skills"
# 未来扩展:
# create_link "../.ai/skills"  "$ROOT/.opencode/skills"

echo ""
echo "Symlink 检查完成。"

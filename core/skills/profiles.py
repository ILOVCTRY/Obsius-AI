"""场景档加载（expert-pool 方案 §4.8 M4a，DESIGN.md §6.6 场景档层）。

packs/tracks/<track>/profiles/<id>.yaml：五件套=组队（experts 预设）/规则模板
（rule_profiles_owners · rule_profiles_rating，F11 三态平铺两键）/剧本（playbook，
预留——pentest-phased-workflow 方案落地后对接）/产物清单（artifacts，预留）/
知识范围（knowledge，预留），外加看板默认视图声明（board_view，M4c）。
解析约定同 experts 平铺 yaml（key: value、内联列表、null=未声明）。

D6 定稿：档是**创建时快照物化**的模板——五件套物化进项目配置后即弃
（experts→meta.experts、rule_profiles/board_view→config、playbook/artifacts/
knowledge 随 config.profile 快照留档），模板后续升级不影响存量项目。

每轨内置 0~3 个（平台预置，文件即档：删文件=退役、新建=扩展）。
"""

from pathlib import Path

from core.skills.experts import _parse_expert

BOARD_VIEWS = ("findings", "assets", "funcs", "board")


def profiles_dir(packs_root: str | Path, track: str) -> Path:
    return Path(packs_root) / "tracks" / track / "profiles"


def load_track_profiles(packs_root: str | Path, track: str) -> list[dict]:
    """轨内置场景档清单（id=文件 stem，按文件名排序；目录不存在=空）。"""
    base = profiles_dir(packs_root, track)
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.glob("*.yaml")):
        d = _parse_expert(p)
        d["id"] = p.stem
        out.append(d)
    return out


def load_profile(packs_root: str | Path, track: str, pid: str) -> dict | None:
    """单个场景档（不存在返 None，调用方 422 提示可用清单）。"""
    path = profiles_dir(packs_root, track) / f"{pid}.yaml"
    if not path.is_file():
        return None
    d = _parse_expert(path)
    d["id"] = pid
    return d


def profile_snapshot(profile: dict) -> dict:
    """物化快照（D6「物化即弃」的留痕件）：整档可消费字段拷进项目 config.profile，
    模板升级/删除不再影响已建项目。"""
    return {
        "id": profile.get("id"),
        "name": profile.get("name") or profile.get("id"),
        "playbook": profile.get("playbook"),
        "artifacts": profile.get("artifacts") or [],
        "knowledge": profile.get("knowledge") or [],
    }

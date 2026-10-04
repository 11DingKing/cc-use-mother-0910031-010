"""分类差异比较：输出可 JSON 序列化的结构化差异。"""
from __future__ import annotations

from .models import RISK_ORDER, ClassificationVersion


def _risk_direction(old: str, new: str) -> str:
    if old == new:
        return "不变"
    old_rank, new_rank = RISK_ORDER.get(old), RISK_ORDER.get(new)
    if old_rank is None or new_rank is None:
        return "调整"
    return "上调" if new_rank > old_rank else "下调"


def _venue_dict(venue) -> dict | None:
    if venue is None:
        return None
    return {"room_type": venue.room_type, "min_area_sqm": venue.min_area_sqm}


def _personnel_dict(req) -> dict:
    return {"role": req.role, "qualification": req.qualification, "min_count": req.min_count}


def _equipment_dict(req) -> dict:
    return {"equipment": req.equipment, "min_count": req.min_count}


def diff_classification(old: ClassificationVersion, new: ClassificationVersion) -> dict:
    """比较同一项目的两个分类版本，列出风险、场地、人员、设备与状态差异。"""
    if old.project_id != new.project_id:
        raise ValueError("只能比较同一项目的分类版本")

    old_personnel = {(p.role, p.qualification): p for p in old.personnel}
    new_personnel = {(p.role, p.qualification): p for p in new.personnel}
    old_equipment = {e.equipment: e for e in old.equipment}
    new_equipment = {e.equipment: e for e in new.equipment}

    personnel_added = [new_personnel[k] for k in sorted(new_personnel.keys() - old_personnel.keys())]
    personnel_removed = [old_personnel[k] for k in sorted(old_personnel.keys() - new_personnel.keys())]
    personnel_changed = [
        (old_personnel[k], new_personnel[k])
        for k in sorted(old_personnel.keys() & new_personnel.keys())
        if old_personnel[k].min_count != new_personnel[k].min_count
    ]
    equipment_added = [new_equipment[k] for k in sorted(new_equipment.keys() - old_equipment.keys())]
    equipment_removed = [old_equipment[k] for k in sorted(old_equipment.keys() - new_equipment.keys())]
    equipment_changed = [
        (old_equipment[k], new_equipment[k])
        for k in sorted(old_equipment.keys() & new_equipment.keys())
        if old_equipment[k].min_count != new_equipment[k].min_count
    ]

    return {
        "project_id": new.project_id,
        "from_version": old.version_no,
        "to_version": new.version_no,
        "risk_level": {
            "from": old.risk_level,
            "to": new.risk_level,
            "direction": _risk_direction(old.risk_level, new.risk_level),
        },
        "venue": {
            "from": _venue_dict(old.venue),
            "to": _venue_dict(new.venue),
            "changed": old.venue != new.venue,
        },
        "personnel_added": [_personnel_dict(p) for p in personnel_added],
        "personnel_removed": [_personnel_dict(p) for p in personnel_removed],
        "personnel_changed": [
            {"requirement": _personnel_dict(new_req), "from_min_count": old_req.min_count}
            for old_req, new_req in personnel_changed
        ],
        "equipment_added": [_equipment_dict(e) for e in equipment_added],
        "equipment_removed": [_equipment_dict(e) for e in equipment_removed],
        "equipment_changed": [
            {"requirement": _equipment_dict(new_req), "from_min_count": old_req.min_count}
            for old_req, new_req in equipment_changed
        ],
        "status": {"from": old.status, "to": new.status},
    }

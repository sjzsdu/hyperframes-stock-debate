"""Validated, data-only video type catalog shared by CLI and application layers."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

import yaml

BUILTINS = Path(__file__).parent
SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class TypeContractError(ValueError):
    pass


@dataclass(frozen=True)
class VideoType:
    declaration: dict[str, Any]
    digest: str

    @property
    def id(self) -> str:
        return str(self.declaration["id"])

    @property
    def strategy(self) -> str:
        return str(self.declaration["script"]["strategy"])

    def public(self) -> dict[str, Any]:
        return {**deepcopy(self.declaration), "digest": self.digest}


def _load(path: Path) -> VideoType:
    raw_declaration = path.read_text(encoding="utf-8")
    if len(raw_declaration) > 65536:
        raise TypeContractError("declaration exceeds size limit")
    d = yaml.safe_load(raw_declaration)
    if not isinstance(d, dict) or d.get("schema_version") != 1:
        raise TypeContractError("schema_version must be 1")
    if not SLUG.fullmatch(str(d.get("id", ""))) or d["id"] != path.parent.name:
        raise TypeContractError("id must be a slug matching its directory")
    for key in ("display_name", "summary"):
        if not isinstance(d.get(key), str) or not d[key].strip():
            raise TypeContractError(f"{key}: nonempty text required")
    if d.get("status") not in ("enabled", "disabled"):
        raise TypeContractError("status: expected enabled or disabled")
    script = d.get("script", {})
    strategy = script.get("strategy")
    from stocktalk.modules.arcs import arcs_for_format
    if strategy not in ("debate", "monologue") or script.get("arc") not in arcs_for_format(strategy):
        raise TypeContractError("script: unknown strategy/arc")
    cast = d.get("cast")
    if not isinstance(cast, list) or len(cast) != (1 if strategy == "monologue" else 2):
        raise TypeContractError("cast: strategy requires one or two speakers")
    ids = []
    for role in cast:
        if not isinstance(role, dict) or not all(isinstance(role.get(k), str) and role[k].strip()
                                                for k in ("speaker_id", "name", "voice_ref")):
            raise TypeContractError("cast: speaker_id/name/voice_ref required")
        if not SLUG.fullmatch(role["speaker_id"]) or not SLUG.fullmatch(role["voice_ref"]):
            raise TypeContractError("cast: invalid speaker_id/voice_ref")
        ids.append(role["speaker_id"])
    if len(set(ids)) != len(ids):
        raise TypeContractError("cast: duplicate speaker_id")
    # Debate strategies assign semantic bull/bear slots; arbitrary IDs are bound at runtime.
    duration = d.get("duration", {})
    lo, hi = duration.get("min_seconds"), duration.get("max_seconds")
    if type(lo) is not int or type(hi) is not int or not 0 < lo <= hi <= 3600:
        raise TypeContractError("duration: expected 0 < min_seconds <= max_seconds <= 3600")
    required = d.get("required_data")
    if not isinstance(required, list) or any(x != "quote" for x in required):
        raise TypeContractError("required_data: only quote is supported")
    visual = d.get("visual", {})
    if not isinstance(visual, dict) or set(visual) != {"scene", "cover"}:
        raise TypeContractError("visual: scene/cover required")
    assets = {}
    root = path.parent.resolve()
    for kind, reference in visual.items():
        if reference in ("debate", "monologue"):
            if reference != strategy:
                raise TypeContractError("visual: layout must match strategy cast")
            continue
        if not isinstance(reference, str):
            raise TypeContractError("visual: expected layout key or local template")
        asset = (root / reference).resolve()
        if Path(reference).is_absolute() or not asset.is_relative_to(root) or not asset.name.endswith(".html.j2"):
            raise TypeContractError("visual: template path escapes type root")
        source = asset.read_text(encoding="utf-8")
        if len(source) > 262144:
            raise TypeContractError("visual: template exceeds limit")
        from .templates import template
        template(source)
        assets[kind] = source
    if assets:
        d["_assets"] = assets
    compliance = d.get("compliance_defaults", {})
    if not all(type(compliance.get(k)) is bool for k in ("ai_declaration", "closing_risk_notice")):
        raise TypeContractError("compliance_defaults: boolean declarations required")
    if not isinstance(compliance.get("tags"), list) or not all(isinstance(t, str) for t in compliance["tags"]):
        raise TypeContractError("compliance_defaults.tags: text array required")
    title = compliance.get("title_template", "")
    if not isinstance(title, str) or re.sub(r"\{\{\s*(stock_name|stock_code)\s*\}\}", "", title).find("{") >= 0:
        raise TypeContractError("title_template: only stock_name/stock_code placeholders supported")
    sample = d.get("sample")
    if not isinstance(sample, str):
        raise TypeContractError("sample: relative script JSON required")
    root = path.parent.resolve()
    sample_path = (root / sample).resolve()
    if Path(sample).is_absolute() or not sample_path.is_relative_to(root) or sample_path.suffix not in (".json", ".mp4"):
        raise TypeContractError("sample: script path escapes type root or has unsupported media type")
    if sample_path.stat().st_size > 10 * 1024 * 1024:
        raise TypeContractError("sample: exceeds size limit")
    raw = sample_path.read_bytes()
    if sample_path.suffix == ".json":
        turns = json.loads(raw).get("turns")
        if not isinstance(turns, list) or not turns or any(not isinstance(t, dict) or t.get("speaker") not in ids
                                                           or not isinstance(t.get("line"), str) or not t["line"].strip() for t in turns):
            raise TypeContractError("sample: invalid turns/speaker")
    else:
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(sample_path), "-frames:v", "1", "-f", "null", "-"],
                           check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise TypeContractError("sample: MP4 decode failed or ffmpeg missing") from None
    prompt = script.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 8000:
        raise TypeContractError("script.prompt: expected text up to 8000 characters")
    digest = hashlib.sha256(path.read_bytes() + raw + json.dumps(assets, sort_keys=True).encode()).hexdigest()
    return VideoType(d, digest)


class TypeCatalog:
    def __init__(self, external: str | Path | None = None):
        self.types: dict[str, VideoType] = {}
        self.diagnostics: dict[str, str] = {}
        seen: set[str] = set()
        for root in [BUILTINS, *([Path(external)] if external else [])]:
            for path in sorted(root.glob("*/type.yaml")):
                key = path.parent.name
                if key in seen:
                    self.types.pop(key, None)
                    self.diagnostics[key] = "duplicate type ID"
                    continue
                seen.add(key)
                try:
                    self.types[key] = _load(path)
                except (ValueError, OSError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
                    self.diagnostics[key] = f"invalid declaration: {exc}"

    def get(self, type_id: str) -> VideoType:
        t = self.types.get(type_id)
        if t is None or t.declaration["status"] != "enabled":
            raise TypeContractError(f"unknown, invalid or disabled video type: {type_id}")
        return t


def selected_type(config: Mapping[str, Any] | None) -> VideoType:
    cfg = config or {}
    if cfg.get("_frozen_type"):
        frozen = deepcopy(cfg["_frozen_type"])
        return VideoType(frozen, frozen.pop("digest"))
    source = cfg.get("dialogue", cfg)
    return TypeCatalog(cfg.get("video_types_dir")).get(str(source.get("type_id") or source.get("format") or "debate"))


def bind_type(config: dict[str, Any]) -> VideoType:
    t = selected_type(config)
    if config.get("_type_digest") == t.digest:
        return t
    d = t.declaration
    characters = deepcopy(config.get("characters", {}))
    if t.id not in ("debate", "monologue"):
        from stocktalk.modules.dialogue_generator import NARRATOR_DEFAULTS
        for role in d["cast"]:
            ref = role["voice_ref"]
            voice = characters.get(ref, NARRATOR_DEFAULTS if ref == "narrator" else None)
            if not voice or not voice.get("voice_id"):
                raise TypeContractError(f"cast.voice_ref: missing configured voice {ref}")
            characters[role["speaker_id"]] = {**deepcopy(voice), "name": role["name"]}
        dialogue = config.setdefault("dialogue", {})
        bounds = {"min_duration_seconds": d["duration"]["min_seconds"],
                  "max_duration_seconds": d["duration"]["max_seconds"]}
        dialogue.update(bounds)
        if t.strategy == "monologue":
            dialogue.setdefault("monologue", {}).update(bounds)
        dialogue["arc"] = d["script"]["arc"]
        config.setdefault("cover", {})["style"] = t.strategy
        publish = config.setdefault("publish", {})
        publish["extra_tags"] = list(dict.fromkeys([*d["compliance_defaults"]["tags"], *publish.get("extra_tags", [])]))
    config["characters"] = characters
    config["_type_digest"] = t.digest
    config["_video_type"] = t.public()
    return t

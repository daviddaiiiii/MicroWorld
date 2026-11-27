"""MicroWorld Visual Prompt storage for OpenFlexure Microscope Server.

Install on the Pi:
  sudo cp org_microworld_visual_prompts.py \\
    /var/openflexure/extensions/microscope_extensions/
  # or place this file and ensure LABTHINGS_EXTENSIONS is discoverable
  sudo systemctl restart openflexure-microscope-server

Authority for visual prompts and agent jobs lives on the microscope host.
Cursor/Codex (or any HTTP client) claim jobs via this API / MCP, drive the
microscope, then POST results for the MicroWorld UI to poll and render.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, send_file
from labthings import fields, find_component
from labthings.extensions import BaseExtension
from labthings.views import PropertyView, View

# OpenFlexure can import extension files directly without adding their directory.
try:
    from microworld_api_compat import legacy_extension
except ImportError:
    from importlib.util import module_from_spec, spec_from_file_location

    _compat_spec = spec_from_file_location(
        "microworld_api_compat", Path(__file__).with_name("microworld_api_compat.py")
    )
    if _compat_spec is None or _compat_spec.loader is None:
        raise
    _compat_module = module_from_spec(_compat_spec)
    _compat_spec.loader.exec_module(_compat_module)
    legacy_extension = _compat_module.legacy_extension

EXTENSION_ID = "org.microworld.visual-prompts"
LEGACY_EXTENSION_ID = "org.openmhs.visual-prompts"
EXTENSION_VERSION = "1.6.0"
DEFAULT_ROOT = Path("/var/openflexure/data/visual-prompts")
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
JOB_LEASE_SECONDS = 120
JOB_STATUSES = (
    "queued",
    "claimed",
    "running",
    "done",
    "failed",
    "cancelled",
)
ACTIONS = {
    "reference",
    "segment",
    "track",
    "compare",
    "focus",
    "measure",
    "agent",
}
ACTION_HELP = {
    "reference": "Identify or explain the selected structure. Distinguish direct visual evidence from uncertainty.",
    "segment": "Concentrate on the selected object boundary. Describe what belongs to the object versus the surrounding background.",
    "track": "Treat the selected structure as the tracking target for later microscope views. Record its distinctive visual features.",
    "compare": "Compare the selected structure with prior visual prompts or reference material when available.",
    "focus": "Assess focus quality inside the selected region and suggest the smallest useful focus adjustment without operating the microscope.",
    "measure": "Estimate or measure the selected structure only if calibration evidence is available; otherwise state what calibration is missing.",
    "agent": "Follow the user note. If a source ROI is present, treat it as visual reference. Apply the operation to the target ROI, or to the full frame when no target ROI is provided.",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def utc_now_dt():
    return datetime.now(timezone.utc)


def parse_iso(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def decode_data_url(value, field_name):
    if not isinstance(value, str) or "," not in value:
        raise ValueError("{} must be an image data URL".format(field_name))
    header, encoded = value.split(",", 1)
    mapping = {
        "data:image/jpeg;base64": ".jpg",
        "data:image/png;base64": ".png",
    }
    extension = mapping.get(header.lower())
    if not extension:
        raise ValueError("{} must be a JPEG or PNG image".format(field_name))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (TypeError, binascii.Error) as exc:
        raise ValueError("{} contains invalid base64 data".format(field_name)) from exc
    if not raw or len(raw) > 12 * 1024 * 1024:
        raise ValueError("{} has an invalid size".format(field_name))
    return raw, extension


def dir_size(path):
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


class VisualPromptStore(object):
    def __init__(self, root=None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.history = self.root / "history"
        self.settings_path = self.root / "settings.json"
        # Ephemeral agent results for web UI only — never written to disk.
        self._live_results = {}
        self.codex_seen = 0
        self.codex_read = None
        # PC scan/Canvas worker presence (RAM; refreshed by heartbeat)
        self._agent_worker = {
            "last_seen": None,
            "worker_id": None,
            "workspace": None,
            "supervisor_url": None,
        }
        self.sessions = self.root / "sessions"
        self.annotations = self.root / "annotations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.annotations.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.exists():
            self.write_settings({"max_bytes": DEFAULT_MAX_BYTES})
        self._purge_persisted_results()
        self._latest_session_id = None
        # Survive Pi restart / brief network blips: remember last PC supervisor
        try:
            settings = self.read_settings()
            last_sup = str(settings.get("pc_supervisor_url") or "").strip()
            if last_sup:
                self._agent_worker["supervisor_url"] = last_sup[:500]
        except Exception:
            pass

    def _purge_persisted_results(self):
        """No-op: slim result.json is kept so folders/UI can reload annotations."""
        return

    def read_settings(self):
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        max_bytes = int(data.get("max_bytes", DEFAULT_MAX_BYTES))
        # Floor at 1 MiB so small test budgets and tiny SD cards remain configurable.
        if max_bytes < 1 * 1024 * 1024:
            max_bytes = 1 * 1024 * 1024
        pc_sup = str(data.get("pc_supervisor_url") or "").strip()[:500] or None
        return {
            "max_bytes": max_bytes,
            "root": str(self.root),
            "pc_supervisor_url": pc_sup,
        }

    def write_settings(self, payload):
        current = self.read_settings()
        if "max_bytes" in payload and payload.get("max_bytes") is not None:
            current["max_bytes"] = int(payload["max_bytes"])
        if "pc_supervisor_url" in payload:
            url = str(payload.get("pc_supervisor_url") or "").strip()[:500]
            current["pc_supervisor_url"] = url or None
        out = {"max_bytes": current["max_bytes"]}
        if current.get("pc_supervisor_url"):
            out["pc_supervisor_url"] = current["pc_supervisor_url"]
        self.settings_path.write_text(
            json.dumps(out, indent=2) + "\n",
            encoding="utf-8",
        )
        self.enforce_quota()
        return self.status()

    def status(self):
        settings = self.read_settings()
        now = time.time()
        used = getattr(self, "_used_bytes_cache", None)
        used_at = float(getattr(self, "_used_bytes_at", 0) or 0)
        if used is None or now - used_at > 30:
            used = dir_size(self.root)
            self._used_bytes_cache = used
            self._used_bytes_at = now
        latest = self.latest_manifest()
        pending = self.list_pending_jobs()
        agent = self.agent_status()
        return {
            "ok": True,
            "extension": EXTENSION_ID,
            "version": "1.6.0",
            "root": str(self.root),
            "max_bytes": settings["max_bytes"],
            "used_bytes": used,
            "free_bytes": max(0, settings["max_bytes"] - used),
            "prompt_count": len(self.list_manifests()),
            "latest_id": None if latest is None else latest.get("id"),
            "pending_jobs": len(pending),
            "latest_job_id": (self.latest_job() or {}).get("id"),
            "results_ephemeral": False,
            "vision_host": "pc-worker",
            "vision_algos": [],
            "agent": agent,
        }

    @staticmethod
    def managed_connection():
        from labthings import current_labthing

        api = current_labthing()
        extension = api.extensions.get("org.microworld.scan-mosaic") if api else None
        if extension is not None:
            return extension.hardware.connection_status()
        return {"managed": False}

    def agent_status(self, stale_after_s=90):
        """Report PC worker connectivity; model/API credentials are not used here."""
        connection = self.managed_connection()
        info = dict(self._agent_worker or {})
        if connection["managed"]:
            online = connection["connected"] and info.get("connection_id") == connection["connection_id"]
            return {
                "ok": True, "connected": online, "healthy": online, "supervisor_ok": online,
                "worker_online": online, "workspace": info.get("workspace") if online else None,
                "worker_id": info.get("worker_id") if online else None,
                "supervisor_url": connection["supervisor_url"], "last_seen": info.get("last_seen") if online else None,
            }
        settings = self.read_settings()
        last = info.get("last_seen")
        age = None
        hb_fresh = False
        if last:
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                hb_fresh = age <= float(stale_after_s)
            except Exception:
                hb_fresh = False
                age = None
        # Prefer live heartbeat URL; fall back to persisted last-known PC URL
        sup = info.get("supervisor_url") or settings.get("pc_supervisor_url")
        supervisor_ok = False
        worker_running = False
        supervisor_error = None
        # Fresh Mac heartbeat is enough. A synchronous Pi→Mac /status probe
        # on every UI poll was blocking Flask and making the microscope look offline.
        if hb_fresh:
            supervisor_ok = True
            worker_running = True
        elif sup:
            try:
                import urllib.request

                probe_url = str(sup).rstrip("/") + "/status"
                with urllib.request.urlopen(probe_url, timeout=0.8) as resp:
                    raw = resp.read()
                data = json.loads(raw.decode("utf-8")) if raw else {}
                if data.get("ok"):
                    supervisor_ok = True
                    worker_running = bool(data.get("worker_running"))
                    if not info.get("workspace") and data.get("workspace"):
                        info["workspace"] = data.get("workspace")
                else:
                    supervisor_error = "supervisor status not ok"
            except Exception as exc:
                supervisor_error = str(exc)[:200]
                logging.debug("PC supervisor probe failed", exc_info=True)

        worker_online = bool(hb_fresh or worker_running)
        error_type = None
        error = None
        if not supervisor_ok and not hb_fresh:
            error_type = "supervisor_down"
            error = supervisor_error or "supervisor unreachable"
        elif not worker_online:
            error_type = "worker_down"

        healthy = bool(supervisor_ok and worker_online)

        return {
            "ok": True,
            "connected": healthy,
            "healthy": healthy,
            "supervisor_ok": bool(supervisor_ok),
            "worker_online": bool(worker_online),
            "error_type": error_type,
            "last_seen": last,
            "age_s": None if age is None else round(age, 1),
            "stale_after_s": stale_after_s,
            "worker_id": info.get("worker_id"),
            "error": error,
            "workspace": info.get("workspace"),
            "supervisor_url": sup,
            "online_via": "heartbeat" if hb_fresh else (
                "supervisor_probe" if worker_running else None
            ),
        }

    def cancel_job(self, job_id):
        """Mark a job cancelled so UI/worker can stop."""
        job = self.read_job(job_id)
        status = job.get("status")
        if status in ("done", "failed", "cancelled"):
            return self.attach_result(job)
        job["status"] = "cancelled"
        job["updated_at"] = utc_now()
        steps = list(job.get("progress") or [])
        steps.append(
            {
                "at": utc_now(),
                "message": "Cancelled by user",
                "kind": "cancel",
                "op": "cancel",
            }
        )
        job["progress"] = steps[-40:]
        self.write_job(job)
        return self.attach_result(job)

    def agent_heartbeat(self, payload):
        payload = payload or {}
        connection = self.managed_connection()
        ws = str(payload.get("workspace") or "").strip()[:500] or None
        sup = str(payload.get("supervisor_url") or "").strip()[:500] or None
        if connection["managed"]:
            sup = connection["supervisor_url"]
        self._agent_worker = {
            "last_seen": utc_now(),
            "connection_id": connection.get("connection_id"),
            "worker_id": str(payload.get("worker_id") or "")[:120] or None,
            "workspace": ws,
            "supervisor_url": sup,
        }
        # Persist PC supervisor URL so Pi restart / brief blips keep /mac/* proxy alive
        if sup and not connection["managed"]:
            try:
                self.write_settings({"pc_supervisor_url": sup})
            except Exception:
                logging.debug("persist pc_supervisor_url failed", exc_info=True)
        # Do not call agent_status() here — it used to probe Mac and stall Flask.
        return {
            "ok": True,
            "last_seen": self._agent_worker.get("last_seen"),
            "worker_id": self._agent_worker.get("worker_id"),
            "supervisor_url": self._agent_worker.get("supervisor_url"),
        }

    def job_path(self, job_id):
        if not re.match(r"^[A-Za-z0-9._-]+$", job_id or ""):
            raise ValueError("Invalid job id")
        path = self.history / job_id / "job.json"
        return path

    def result_path(self, job_id):
        return self.history / job_id / "result.json"

    def read_job(self, job_id):
        path = self.job_path(job_id)
        if not path.exists():
            raise FileNotFoundError("Job not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def write_job(self, job):
        """Persist job metadata; keep slim result.json alongside (written separately)."""
        job_id = job["id"]
        path = self.job_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        disk = dict(job)
        meta = disk.get("result")
        if isinstance(meta, dict):
            disk["result"] = {
                k: meta.get(k)
                for k in ("summary", "annotation_count", "completed_at", "path", "error")
                if meta.get(k) is not None
            } or None
        else:
            disk["result"] = None
        path.write_text(
            json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (self.root / "latest-job.json").write_text(
            json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return job

    def attach_result(self, job):
        """Attach in-memory result for UI poll; fall back to slim result.json."""
        out = dict(job)
        live = self._live_results.get(job["id"])
        if live is not None:
            out["result"] = live
            return out
        rpath = self.result_path(job["id"])
        if rpath.exists():
            try:
                out["result"] = json.loads(rpath.read_text(encoding="utf-8"))
                return out
            except (OSError, ValueError):
                pass
        out["result"] = None
        return out

    def expire_stale_claims(self, job):
        """Return job, flipping expired claimed/running leases back to queued."""
        status = job.get("status")
        if status not in ("claimed", "running"):
            return job
        claim = job.get("claim") or {}
        until = parse_iso(claim.get("lease_until"))
        if until and utc_now_dt() > until:
            job = dict(job)
            job["status"] = "queued"
            job["claim"] = None
            job["updated_at"] = utc_now()
            self.write_job(job)
        return job

    def list_jobs(self):
        items = []
        for path in sorted(self.history.glob("*/job.json"), reverse=True):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                job = self.expire_stale_claims(job)
                items.append(self.attach_result(job))
            except (OSError, ValueError):
                continue
        return items

    def list_pending_jobs(self):
        pending = []
        for job in self.list_jobs():
            if job.get("status") == "queued":
                pending.append(job)
        return pending

    def latest_job(self):
        path = self.root / "latest-job.json"
        if path.exists():
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                job = self.expire_stale_claims(job)
                return self.attach_result(job)
            except (OSError, ValueError):
                pass
        jobs = self.list_jobs()
        return jobs[0] if jobs else None

    def get_job(self, job_id):
        job = self.expire_stale_claims(self.read_job(job_id))
        return self.attach_result(job)

    def create_job_for_prompt(self, manifest):
        job_id = manifest["id"]
        now = utc_now()
        req = {
            "note": manifest.get("note") or "",
            "action": manifest.get("action") or "agent",
            "rois": manifest.get("rois") or [],
            "roi": manifest.get("roi"),
            "surface": manifest.get("surface") or "live",
            "position": manifest.get("position"),
        }
        if manifest.get("session_id"):
            req["session_id"] = manifest.get("session_id")
        if manifest.get("view_id"):
            req["view_id"] = manifest.get("view_id")
        if manifest.get("record_id"):
            req["record_id"] = manifest.get("record_id")
        if manifest.get("parent_job_id"):
            req["parent_job_id"] = manifest.get("parent_job_id")
        if manifest.get("active_model_id"):
            req["active_model_id"] = manifest.get("active_model_id")
        job = {
            "id": job_id,
            "prompt_id": job_id,
            "session_id": manifest.get("session_id"),
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "claim": None,
            "request": req,
            "result": None,
            "progress": [],
        }
        self.write_job(job)
        return job

    def claim_job(self, job_id, worker_id, lease_seconds=None):
        worker_id = str(worker_id or "").strip() or "anonymous"
        lease_seconds = int(lease_seconds or JOB_LEASE_SECONDS)
        lease_seconds = max(30, min(600, lease_seconds))
        job = self.expire_stale_claims(self.read_job(job_id))
        if job.get("status") == "cancelled":
            raise RuntimeError("Job was cancelled")
        if job.get("status") not in ("queued",):
            # Allow same worker to re-claim its own lease
            claim = job.get("claim") or {}
            if not (
                job.get("status") in ("claimed", "running")
                and claim.get("worker_id") == worker_id
            ):
                raise RuntimeError(
                    "Job is not claimable (status={})".format(job.get("status"))
                )
        now = utc_now_dt()
        job["status"] = "claimed"
        job["claim"] = {
            "worker_id": worker_id,
            "claimed_at": now.isoformat(),
            "lease_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
        }
        job["updated_at"] = utc_now()
        self.write_job(job)
        return self.attach_result(job)

    def _clean_progress_step(self, step):
        if not isinstance(step, dict):
            return None
        kind = str(step.get("kind") or "")[:32]
        # Stream chunks need a longer buffer so the UI can type out replies
        msg_limit = 8000 if kind == "stream" else 500
        out = {
            "at": str(step.get("at") or utc_now())[:64],
            "message": str(step.get("message") or step.get("text") or "")[:msg_limit],
        }
        if step.get("op"):
            out["op"] = str(step.get("op"))[:80]
        if step.get("detail"):
            out["detail"] = str(step.get("detail"))[:400]
        if kind:
            out["kind"] = kind
        if not out["message"] and out.get("op"):
            out["message"] = out["op"]
        if not out["message"]:
            return None
        return out

    def heartbeat_job(
        self, job_id, worker_id, lease_seconds=None, progress=None, progress_step=None
    ):
        worker_id = str(worker_id or "").strip() or "anonymous"
        lease_seconds = int(lease_seconds or JOB_LEASE_SECONDS)
        lease_seconds = max(30, min(600, lease_seconds))
        job = self.expire_stale_claims(self.read_job(job_id))
        claim = job.get("claim") or {}
        if job.get("status") == "cancelled":
            raise RuntimeError("Job was cancelled")
        if job.get("status") not in ("claimed", "running"):
            raise RuntimeError("Job has no active claim")
        if claim.get("worker_id") != worker_id:
            raise RuntimeError("Worker does not own this job claim")
        now = utc_now_dt()
        job["status"] = "running"
        job["claim"] = {
            **claim,
            "lease_until": (now + timedelta(seconds=lease_seconds)).isoformat(),
        }
        # Live process trace for Agent&Tool expandable panel
        if isinstance(progress, list):
            cleaned = []
            for item in progress[-40:]:
                step = self._clean_progress_step(item)
                if step:
                    cleaned.append(step)
            job["progress"] = cleaned
        elif isinstance(progress_step, dict):
            steps = list(job.get("progress") or [])
            step = self._clean_progress_step(progress_step)
            if step:
                # Replace last stream step so token streaming doesn't fill the ring buffer
                if (
                    step.get("kind") == "stream"
                    and steps
                    and (steps[-1] or {}).get("kind") == "stream"
                ):
                    steps[-1] = step
                else:
                    steps.append(step)
                job["progress"] = steps[-40:]
        job["updated_at"] = utc_now()
        self.write_job(job)
        return self.attach_result(job)

    def submit_job_result(self, job_id, payload, worker_id=None):
        job = self.expire_stale_claims(self.read_job(job_id))
        force = bool(payload.get("force"))
        if job.get("status") in ("done", "cancelled") and not force:
            raise RuntimeError("Job already finished")
        claim = job.get("claim") or {}
        if (
            worker_id
            and claim.get("worker_id")
            and claim.get("worker_id") != worker_id
            and not force
        ):
            raise RuntimeError("Worker does not own this job claim")

        status = str(payload.get("status") or "done").strip().lower()
        if status not in ("done", "failed"):
            status = "done" if not payload.get("error") else "failed"

        annotations = payload.get("annotations") or []
        if not isinstance(annotations, list):
            raise ValueError("annotations must be a list")
        clean_ann = []
        for item in annotations:
            if not isinstance(item, dict):
                continue
            ann = {
                "type": str(item.get("type") or "bbox").strip().lower(),
                "label": str(item.get("label") or "")[:500],
                "role": str(item.get("role") or "result").strip().lower(),
            }
            if item.get("color"):
                ann["color"] = str(item.get("color"))[:32]
            if item.get("norm") and isinstance(item["norm"], dict):
                try:
                    ann["norm"] = {
                        k: float(item["norm"][k])
                        for k in ("x", "y", "width", "height")
                    }
                except (KeyError, TypeError, ValueError):
                    pass
            elif all(k in item for k in ("x", "y", "width", "height")):
                try:
                    ann["norm"] = {
                        k: float(item[k]) for k in ("x", "y", "width", "height")
                    }
                except (TypeError, ValueError):
                    pass
            if item.get("points"):
                ann["points"] = item.get("points")
            if item.get("text"):
                ann["text"] = str(item.get("text"))[:2000]
            if item.get("area_px") is not None:
                try:
                    ann["area_px"] = float(item.get("area_px"))
                except (TypeError, ValueError):
                    pass
            clean_ann.append(ann)

        mask_overlay = payload.get("mask_overlay_data_url")
        if mask_overlay is not None:
            mask_overlay = str(mask_overlay)
            # Keep RAM-only overlays bounded (~1.5MB base64)
            if len(mask_overlay) > 1_500_000:
                mask_overlay = None

        result = {
            "summary": str(payload.get("summary") or "")[:2000],
            "markdown": str(payload.get("markdown") or payload.get("summary") or "")[
                :20000
            ],
            "annotations": clean_ann,
            "mask_overlay_data_url": mask_overlay,
            "actions_log": payload.get("actions_log")
            if isinstance(payload.get("actions_log"), list)
            else [],
            "error": str(payload.get("error") or "")[:2000] or None,
            "completed_at": utc_now(),
            "worker_id": (claim.get("worker_id") if claim else None)
            or worker_id
            or None,
            "ephemeral": False,
        }
        # Keep full result (incl. optional mask overlay) in RAM for UI poll.
        self._live_results[job_id] = result
        # Also persist a slim result.json (no huge data-URL) so folders show
        # the analysis after the browser leaves / Pi restarts.
        slim = {
            "summary": result["summary"],
            "markdown": result["markdown"],
            "annotations": result["annotations"],
            "actions_log": result.get("actions_log") or [],
            "error": result.get("error"),
            "completed_at": result.get("completed_at"),
            "worker_id": result.get("worker_id"),
            "has_mask_overlay": bool(result.get("mask_overlay_data_url")),
        }
        try:
            job_dir = self.history / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "result.json").write_text(
                json.dumps(slim, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logging.getLogger("openmhs.visual_prompts").exception(
                "write slim result.json"
            )
        job["status"] = status
        job["updated_at"] = utc_now()
        job["result"] = {
            "summary": slim["summary"],
            "annotation_count": len(slim["annotations"]),
            "completed_at": slim.get("completed_at"),
            "path": "result.json",
        }
        if job.get("session_id") or (job.get("request") or {}).get("session_id"):
            try:
                self.apply_job_result_to_session(job_id, result)
            except Exception:
                logging.getLogger("openmhs.visual_prompts").exception(
                    "session update after result"
                )
        self.write_job(job)
        return self.attach_result(job)

    def run_algorithm_for_job(self, job_id, action=None):
        """Removed — segmentation/tracking tools are being redesigned."""
        del job_id, action
        return None

    def list_manifests(self):
        items = []
        for path in sorted(self.history.glob("*/manifest.json"), reverse=True):
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return items

    def latest_manifest(self):
        path = self.root / "latest.json"
        if not path.exists():
            manifests = self.list_manifests()
            return manifests[0] if manifests else None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def get_manifest(self, prompt_id):
        if not re.match(r"^[A-Za-z0-9._-]+$", prompt_id or ""):
            raise ValueError("Invalid prompt id")
        path = self.history / prompt_id / "manifest.json"
        if not path.exists():
            raise FileNotFoundError("Visual prompt not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def file_for(self, prompt_id, kind):
        manifest = self.get_manifest(prompt_id) if prompt_id != "latest" else self.latest_manifest()
        if not manifest:
            raise FileNotFoundError("Visual prompt not found")
        key = {
            "frame": "frame_path",
            "roi": "roi_path",
            "prompt": "prompt_path",
            "vp": "vp_path",
        }.get(kind)
        if not key:
            raise ValueError("kind must be frame, roi, prompt, or vp")
        path_str = manifest.get(key)
        if not path_str:
            raise FileNotFoundError("Visual prompt file missing")
        path = Path(path_str)
        if not path.exists():
            raise FileNotFoundError("Visual prompt file missing")
        return path, manifest

    def enforce_quota(self):
        max_bytes = self.read_settings()["max_bytes"]
        # Delete oldest history entries first (by id / name). Keep at least the newest one.
        while dir_size(self.root) > max_bytes:
            dirs = sorted([p for p in self.history.iterdir() if p.is_dir()], key=lambda p: p.name)
            if len(dirs) <= 1:
                break
            shutil.rmtree(str(dirs[0]), ignore_errors=True)

    def create(self, payload):
        action = str(payload.get("action") or "reference").strip().lower()
        if action not in ACTIONS:
            raise ValueError("Unknown visual prompt action")
        note = str(payload.get("note") or "").strip()[:50000]
        rois_in = payload.get("rois")
        roi = payload.get("roi")
        normalized = None
        rois_norm = []

        if isinstance(rois_in, list) and rois_in:
            for item in rois_in:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "target").strip().lower()
                if role not in ("target", "source"):
                    role = "target"
                shape = str(item.get("shape") or "bbox").strip().lower()
                try:
                    entry = {
                        "role": role,
                        "shape": shape,
                        "x": float(item.get("x", 0)),
                        "y": float(item.get("y", 0)),
                        "width": float(item.get("width", 1)),
                        "height": float(item.get("height", 1)),
                    }
                except (TypeError, ValueError) as exc:
                    raise ValueError("ROI coordinates are invalid") from exc
                if item.get("points"):
                    entry["points"] = item.get("points")
                rois_norm.append(entry)
            target = next((r for r in rois_norm if r["role"] == "target"), None)
            if target is None and rois_norm:
                target = dict(rois_norm[0])
                target["role"] = "target"
            if target is None:
                target = {
                    "role": "target",
                    "shape": "bbox",
                    "x": 0.0,
                    "y": 0.0,
                    "width": 1.0,
                    "height": 1.0,
                }
                rois_norm.append(target)
            normalized = {
                "x": target["x"],
                "y": target["y"],
                "width": target["width"],
                "height": target["height"],
            }
        elif isinstance(roi, dict):
            try:
                normalized = {name: float(roi[name]) for name in ("x", "y", "width", "height")}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("ROI coordinates are invalid") from exc
            rois_norm = [
                {
                    "role": "target",
                    "shape": "bbox",
                    **normalized,
                }
            ]
        else:
            # Whole frame as target
            normalized = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
            rois_norm = [
                {"role": "target", "shape": "bbox", **normalized}
            ]

        if (
            normalized["x"] < 0
            or normalized["y"] < 0
            or normalized["width"] <= 0
            or normalized["height"] <= 0
            or normalized["x"] + normalized["width"] > 1.0001
            or normalized["y"] + normalized["height"] > 1.0001
        ):
            raise ValueError("ROI coordinates must be normalized between zero and one")

        frame, frame_ext = decode_data_url(payload.get("frame_data_url"), "frame_data_url")
        crop_url = payload.get("roi_data_url")
        if crop_url:
            crop, crop_ext = decode_data_url(crop_url, "roi_data_url")
        else:
            crop, crop_ext = frame, frame_ext

        prompt_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        prompt_dir = self.history / prompt_id
        prompt_dir.mkdir(parents=True, exist_ok=False)
        frame_path = prompt_dir / ("frame" + frame_ext)
        roi_path = prompt_dir / ("roi" + crop_ext)
        frame_path.write_bytes(frame)
        roi_path.write_bytes(crop)

        vp_path = None
        vp_url = payload.get("vp_overlay_data_url")
        if vp_url:
            try:
                vp_bytes, vp_ext = decode_data_url(vp_url, "vp_overlay_data_url")
                vp_path = prompt_dir / ("vp" + vp_ext)
                vp_path.write_bytes(vp_bytes)
            except Exception:
                LOG = logging.getLogger("openmhs.visual_prompts")
                LOG.exception("failed to store VP overlay")
                vp_path = None

        position = None
        try:
            microscope = find_component("org.openflexure.microscope")
            state = microscope.state if hasattr(microscope, "state") else None
            if callable(state):
                state = state()
            if isinstance(state, dict):
                position = (state.get("stage") or {}).get("position")
            elif hasattr(microscope, "stage") and microscope.stage is not None:
                position = getattr(microscope.stage, "position", None)
        except Exception:
            position = None

        created_at = utc_now()
        help_text = ACTION_HELP[action]
        has_source = any(r.get("role") == "source" for r in rois_norm)
        surface = str(payload.get("surface") or "live")
        prompt_text = (
            "MicroWorld visual prompt `{id}` on the microscope ({surface}). "
            "Primary ROI image: use MCP get_latest_visual_prompt or GET .../latest/roi. "
            "Full frame: GET .../latest/frame. {help}"
        ).format(id=prompt_id, surface=surface, help=help_text)
        if has_source:
            prompt_text += " A source ROI is provided as visual reference for the target."
        if note:
            prompt_text += " User note: {}".format(note)

        markdown = "\n".join(
            [
                "# MicroWorld Visual Prompt",
                "",
                prompt_text,
                "",
                "## Context",
                "",
                "- Created: `{}`".format(created_at),
                "- Shortcut: `{}`".format(action),
                "- Surface: `{}`".format(surface),
                "- Stage XYZ: `{}`".format(json.dumps(position, ensure_ascii=False)),
                "- Primary ROI normalized: `{}`".format(
                    json.dumps(normalized, ensure_ascii=False)
                ),
                "- ROIs: `{}`".format(json.dumps(rois_norm, ensure_ascii=False)),
                "- Prompt id: `{}`".format(prompt_id),
                "- User note: {}".format(note or "(none)"),
                "",
                "## Agent access",
                "",
                "- Claim job via MCP `list_pending_agent_jobs` / `claim_agent_job` or HTTP `/jobs/pending`.",
                "- Analyze with `get_visual_prompt` / `get_latest_visual_prompt`.",
                "- Drive microscope with MCP stage/autofocus tools when the note requires it.",
                "- Always finish with `submit_agent_job_result` (markdown + annotations).",
                "",
            ]
        )
        prompt_path = prompt_dir / "prompt.md"
        if not payload.get("queue_agent", True):
            markdown = markdown.split("## Agent access")[0] + (
                "## Codex access\n\nRead this immutable prompt with get_visual_prompt. "
                "Present results in the native Codex conversation. No worker job was queued.\n"
            )
        if payload.get("selection_cleared"):
            markdown = "# MicroWorld visual context\n\nThe user cleared the selection. No region is currently selected. The frame is context only; do not reuse an older ROI.\n"
        prompt_path.write_text(markdown, encoding="utf-8")

        manifest = {
            "context_only": bool(payload.get("context_only")),
            "selection_cleared": bool(payload.get("selection_cleared")),
            "canvas_revision": payload.get("canvas_revision"),
            "canvas_bounds": payload.get("canvas_bounds"),
            "canvas_reference": payload.get("canvas_reference"),
            "image_adjustments": payload.get("image_adjustments"),
            "id": prompt_id,
            "created_at": created_at,
            "action": action,
            "note": note,
            "roi": None if payload.get("selection_cleared") else normalized,
            "rois": [] if payload.get("selection_cleared") else rois_norm,
            "surface": surface,
            "position": position,
            "frame_path": str(frame_path),
            "roi_path": str(roi_path),
            "vp_path": str(vp_path) if vp_path else None,
            "prompt_path": str(prompt_path),
            "frame_name": frame_path.name,
            "roi_name": roi_path.name,
            "job_id": prompt_id if payload.get("queue_agent", True) else None,
        }
        view_id = str(payload.get("view_id") or "").strip()
        record_id = str(payload.get("record_id") or "").strip()
        if view_id:
            manifest["view_id"] = view_id
        if record_id:
            manifest["record_id"] = record_id
        (prompt_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # Latest mirrors for simple HTTP/MCP reads
        shutil.copyfile(frame_path, self.root / ("latest-frame" + frame_ext))
        shutil.copyfile(roi_path, self.root / ("latest-roi" + crop_ext))
        if vp_path and vp_path.exists():
            shutil.copyfile(vp_path, self.root / ("latest-vp" + vp_path.suffix))
        (self.root / "latest.md").write_text(markdown, encoding="utf-8")
        (self.root / "latest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # Remove other latest-* variants with different extensions
        for pattern in ("latest-frame.*", "latest-roi.*"):
            for path in self.root.glob(pattern):
                if path.name not in {
                    "latest-frame" + frame_ext,
                    "latest-roi" + crop_ext,
                }:
                    try:
                        path.unlink()
                    except OSError:
                        pass

        job = self.create_job_for_prompt(manifest) if payload.get("queue_agent", True) else None
        # Vision + agent jobs stay queued for Mac worker (no Pi-side OpenCV auto-run).
        self.enforce_quota()
        return {
            **manifest,
            "prompt": prompt_text,
            "job_id": job["id"] if isinstance(job, dict) else None,
            "job": job,
            "action": action,
            "algo": False,
            "queued_for_mac": job is not None,
            "links": {
                "latest": "/api/v2/extensions/{}/latest".format(EXTENSION_ID),
                "latest_frame": "/api/v2/extensions/{}/latest/frame".format(EXTENSION_ID),
                "latest_roi": "/api/v2/extensions/{}/latest/roi".format(EXTENSION_ID),
                "latest_prompt": "/api/v2/extensions/{}/latest/prompt".format(EXTENSION_ID),
                "job": "/api/v2/extensions/{}/jobs/{}".format(
                    EXTENSION_ID, job["id"] if isinstance(job, dict) else manifest["id"]
                ),
                "jobs_pending": "/api/v2/extensions/{}/jobs/pending".format(EXTENSION_ID),
                "jobs_latest": "/api/v2/extensions/{}/jobs/latest".format(EXTENSION_ID),
            },
            "storage": self.status(),
        }


    # ---- Agent sessions (multi-turn) ----
    def _session_path(self, session_id):
        return self.sessions / session_id / "session.json"

    def create_session(self, note="", view_id=None):
        sid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        now = utc_now()
        vid = str(view_id or "").strip() or None
        session = {
            "id": sid,
            "created_at": now,
            "updated_at": now,
            "status": "idle",
            "view_id": vid,
            "note": str(note or ""),
            "messages": [],
            "thread_items": [],
            "rois": [],
            "draft_annotations": [],
            "draft_mask_path": None,
            "active_model_id": None,
            "latest_prompt_id": None,
            "latest_job_id": None,
        }
        d = self.sessions / sid
        d.mkdir(parents=True, exist_ok=True)
        self._write_session(session)
        self._latest_session_id = sid
        latest_payload = {"id": sid}
        if vid:
            latest_payload["view_id"] = vid
        (self.root / "latest-session.json").write_text(
            json.dumps(latest_payload, indent=2) + "\n", encoding="utf-8"
        )
        return session

    def find_session_by_view_id(self, view_id):
        """Return the newest session bound to a Camera (view_id), or None."""
        vid = str(view_id or "").strip()
        if not vid or not self.sessions.is_dir():
            return None
        best = None
        best_mtime = -1.0
        for child in self.sessions.iterdir():
            if not child.is_dir():
                continue
            path = child / "session.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(data.get("view_id") or "").strip() != vid:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if mtime >= best_mtime:
                best_mtime = mtime
                best = data.get("id") or child.name
        if not best:
            return None
        try:
            return self.read_session(best)
        except FileNotFoundError:
            return None

    def _write_session(self, session):
        sid = session["id"]
        path = self._session_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Do not persist huge mask data URLs in session.json
        slim = dict(session)
        slim.pop("draft_mask_data_url", None)
        path.write_text(
            json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def read_session(self, session_id):
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(session_id)
        session = json.loads(path.read_text(encoding="utf-8"))
        mask_path = session.get("draft_mask_path")
        if mask_path:
            p = Path(mask_path)
            if not p.is_absolute():
                p = self.sessions / session_id / p
            if p.exists():
                try:
                    raw = p.read_bytes()
                    import base64 as _b64
                    session["draft_mask_data_url"] = (
                        "data:image/png;base64," + _b64.b64encode(raw).decode("ascii")
                    )
                except OSError:
                    pass
        return session

    def latest_session(self):
        latest = self.root / "latest-session.json"
        if latest.exists():
            try:
                sid = json.loads(latest.read_text(encoding="utf-8")).get("id")
                if sid:
                    return self.read_session(sid)
            except (OSError, ValueError, FileNotFoundError):
                pass
        return None

    def append_session_message(self, session_id, role, content, extra=None):
        session = self.read_session(session_id)
        msg = {
            "role": str(role),
            "content": str(content or "")[:50000],
            "at": utc_now(),
        }
        if isinstance(extra, dict):
            for k in ("job_id", "prompt_id", "kind"):
                if extra.get(k):
                    msg[k] = extra[k]
        messages = list(session.get("messages") or [])
        messages.append(msg)
        session["messages"] = messages[-500:]
        session["updated_at"] = utc_now()
        self._write_session(session)
        return session

    def set_session_thread_items(self, session_id, thread_items):
        """Persist slim LLM thread for Codex-like multi-turn continuity."""
        session = self.read_session(session_id)
        items = []
        if isinstance(thread_items, list):
            for m in thread_items[-400:]:
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "")
                if role not in ("system", "user", "assistant", "tool"):
                    continue
                entry = {"role": role}
                content = m.get("content")
                if isinstance(content, list):
                    # Already-slim parts preferred
                    entry["content"] = content
                else:
                    entry["content"] = str(content or "")[:50000]
                if m.get("tool_calls"):
                    entry["tool_calls"] = m.get("tool_calls")
                if m.get("tool_call_id"):
                    entry["tool_call_id"] = m.get("tool_call_id")
                if m.get("name"):
                    entry["name"] = m.get("name")
                if m.get("compaction"):
                    entry["compaction"] = True
                items.append(entry)
        session["thread_items"] = items
        session["updated_at"] = utc_now()
        self._write_session(session)
        return session

    def update_session_draft(self, session_id, annotations=None, mask_data_url=None,
                             status=None, active_model_id=None, latest_job_id=None,
                             latest_prompt_id=None, rois=None):
        session = self.read_session(session_id)
        if annotations is not None and isinstance(annotations, list):
            session["draft_annotations"] = annotations[:64]
        if rois is not None and isinstance(rois, list):
            session["rois"] = rois
        if status:
            session["status"] = str(status)[:32]
        if active_model_id is not None:
            session["active_model_id"] = active_model_id or None
        if latest_job_id:
            session["latest_job_id"] = latest_job_id
        if latest_prompt_id:
            session["latest_prompt_id"] = latest_prompt_id
        if mask_data_url:
            try:
                raw, ext = decode_data_url(mask_data_url, "mask")
                fname = "draft_mask" + (ext if ext in (".png", ".jpg") else ".png")
                fpath = self.sessions / session_id / fname
                fpath.write_bytes(raw)
                session["draft_mask_path"] = fname
            except Exception:
                logging.getLogger("openmhs.visual_prompts").exception("draft mask save")
        session["updated_at"] = utc_now()
        self._write_session(session)
        return self.read_session(session_id)

    def post_session_turn(self, session_id, payload):
        """Save prompt images, append user message, queue session-bound job."""
        session = self.read_session(session_id)
        payload = dict(payload or {})
        payload.setdefault("action", "agent")
        note = str(payload.get("note") or "").strip()
        # Reuse create() pipeline but inject session_id
        result = self.create(payload)
        prompt_id = result.get("id") or result.get("job_id")
        job = result.get("job") or {}
        job_id = result.get("job_id") or (job.get("id") if isinstance(job, dict) else prompt_id)
        # Patch job request with session linkage
        try:
            j = self.read_job(job_id)
            j["session_id"] = session_id
            req = dict(j.get("request") or {})
            req["session_id"] = session_id
            req["parent_job_id"] = session.get("latest_job_id")
            if payload.get("view_id"):
                req["view_id"] = payload.get("view_id")
            if payload.get("record_id"):
                req["record_id"] = payload.get("record_id")
            if session.get("active_model_id"):
                req["active_model_id"] = session.get("active_model_id")
            j["request"] = req
            self.write_job(j)
            job = j
        except Exception:
            logging.getLogger("openmhs.visual_prompts").exception("link session job")
        self.append_session_message(
            session_id, "user", note, {"job_id": job_id, "prompt_id": prompt_id}
        )
        self.update_session_draft(
            session_id,
            status="running",
            latest_job_id=job_id,
            latest_prompt_id=prompt_id,
            rois=payload.get("rois"),
        )
        result["session_id"] = session_id
        result["job"] = job
        return result

    def apply_job_result_to_session(self, job_id, result_payload):
        job = self.read_job(job_id)
        sid = job.get("session_id") or (job.get("request") or {}).get("session_id")
        if not sid:
            return None
        summary = (result_payload or {}).get("summary") or ""
        markdown = (result_payload or {}).get("markdown") or summary
        self.append_session_message(
            sid,
            "assistant",
            markdown[:8000] or summary,
            {"job_id": job_id, "kind": "result"},
        )
        return self.update_session_draft(
            sid,
            annotations=(result_payload or {}).get("annotations"),
            mask_data_url=(result_payload or {}).get("mask_overlay_data_url"),
            status="awaiting_user",
            latest_job_id=job_id,
        )

    # ---- Annotation store (training data) ----
    def add_annotation(self, payload):
        aid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        d = self.annotations / aid
        d.mkdir(parents=True, exist_ok=True)
        img_url = payload.get("image_data_url") or payload.get("roi_data_url")
        if not img_url:
            raise ValueError("image_data_url required")
        raw, ext = decode_data_url(img_url, "image_data_url")
        img_name = "image" + ext
        (d / img_name).write_bytes(raw)
        masks = payload.get("masks") or payload.get("annotations") or []
        mask_url = payload.get("mask_data_url")
        mask_name = None
        if mask_url:
            mraw, mext = decode_data_url(mask_url, "mask_data_url")
            mask_name = "mask" + mext
            (d / mask_name).write_bytes(mraw)
        meta = {
            "id": aid,
            "created_at": utc_now(),
            "session_id": payload.get("session_id"),
            "model_id": payload.get("model_id"),
            "split": str(payload.get("split") or "train"),
            "note": str(payload.get("note") or "")[:50000],
            "image": img_name,
            "mask": mask_name,
            "annotations": masks if isinstance(masks, list) else [],
            "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return meta

    def list_annotations(self, limit=50):
        items = []
        for path in sorted(self.annotations.glob("*/meta.json"), reverse=True)[:limit]:
            try:
                items.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return items

    def export_cellpose_dataset(self, split="train", dest_name=None):
        """Export annotations to Cellpose *_img / *_masks directory on Pi."""
        dest_name = dest_name or ("cellpose-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        dest = self.annotations / "_export" / dest_name
        dest.mkdir(parents=True, exist_ok=True)
        n = 0
        for meta in self.list_annotations(limit=500):
            if split and meta.get("split") and meta.get("split") != split:
                # still allow unlabeled
                if meta.get("split") not in (split, None, ""):
                    continue
            src = self.annotations / meta["id"]
            img = src / (meta.get("image") or "image.jpg")
            if not img.exists():
                continue
            stem = "{:04d}".format(n)
            # Always copy image as *_img
            img_dest = dest / (stem + "_img" + img.suffix)
            img_dest.write_bytes(img.read_bytes())
            mask_name = meta.get("mask")
            if mask_name and (src / mask_name).exists():
                msrc = src / mask_name
                (dest / (stem + "_masks" + msrc.suffix)).write_bytes(msrc.read_bytes())
            else:
                # Write empty placeholder note for missing masks
                (dest / (stem + "_masks_missing.txt")).write_text(
                    "no mask for {}\n".format(meta["id"]), encoding="utf-8"
                )
            n += 1
        info = {
            "ok": True,
            "path": str(dest),
            "n": n,
            "split": split,
            "hint": "Copy this folder to the PC Worker and call start_training(dataset_dir=...)",
        }
        (dest / "export.json").write_text(
            json.dumps(info, indent=2) + "\n", encoding="utf-8"
        )
        return info



class VisualPromptsExtension(BaseExtension):
    def __init__(self):
        super(VisualPromptsExtension, self).__init__(EXTENSION_ID, version=EXTENSION_VERSION)
        self.store = VisualPromptStore()
        self.register_views()

    def register_views(self):
        self.add_view(StatusView, "/status")
        self.add_view(CodexStatusView, "/codex")
        self.add_view(CodexPromptsView, "/codex/prompts")
        self.add_view(SettingsView, "/settings")
        self.add_view(AgentStatusView, "/agent")
        self.add_view(AgentHeartbeatView, "/agent/heartbeat")
        self.add_view(PromptsView, "/prompts")
        self.add_view(PromptItemView, "/prompts/<string:prompt_id>")
        self.add_view(PromptFileView, "/prompts/<string:prompt_id>/<string:kind>")
        self.add_view(LatestView, "/latest")
        self.add_view(LatestFileView, "/latest/<string:kind>")
        self.add_view(JobsPendingView, "/jobs/pending")
        self.add_view(JobsLatestView, "/jobs/latest")
        self.add_view(JobItemView, "/jobs/<string:job_id>")
        self.add_view(JobClaimView, "/jobs/<string:job_id>/claim")
        self.add_view(JobHeartbeatView, "/jobs/<string:job_id>/heartbeat")
        self.add_view(JobCancelView, "/jobs/<string:job_id>/cancel")
        self.add_view(JobResultView, "/jobs/<string:job_id>/result")
        self.add_view(JobRunAlgoView, "/jobs/<string:job_id>/run_algo")
        self.add_view(SessionsView, "/sessions")
        self.add_view(SessionsLatestView, "/sessions/latest")
        self.add_view(SessionItemView, "/sessions/<string:session_id>")
        self.add_view(SessionMessagesView, "/sessions/<string:session_id>/messages")
        self.add_view(SessionThreadView, "/sessions/<string:session_id>/thread")
        self.add_view(AnnotationsView, "/annotations")
        self.add_view(AnnotationsExportView, "/annotations/export")


class CodexStatusView(View):
    def get(self):
        store = self.extension.store
        worker = store._agent_worker or {}
        last = worker.get("last_seen")
        fresh = False
        if last:
            try:
                fresh = (utc_now_dt() - datetime.fromisoformat(last)).total_seconds() < 90
            except (ValueError, TypeError):
                pass
        return {"connected": time.time() - store.codex_seen < 45,
                "last_read": store.codex_read, "worker_online": fresh,
                "workspace": worker.get("workspace")}

    def post(self):
        from flask import request
        payload = request.get_json(silent=True) or {}
        store = self.extension.store
        store.codex_seen = time.time()
        if payload.get("read"):
            store.codex_read = utc_now()
        return self.get()


class CodexPromptsView(View):
    def post(self):
        from flask import request
        payload = dict(request.get_json(silent=True) or {})
        payload["queue_agent"] = False
        try:
            return self.extension.store.create(payload), 201
        except ValueError as exc:
            return {"error": str(exc)}, 400


class AgentStatusView(PropertyView):
    def get(self):
        """PC worker and supervisor connectivity for the microscope UI."""
        return self.extension.store.agent_status()


class AgentHeartbeatView(View):
    def post(self):
        """Receive the PC worker's connection and workspace heartbeat."""
        from flask import request

        payload = request.get_json(silent=True) or {}
        return self.extension.store.agent_heartbeat(payload)


class JobRunAlgoView(View):
    def post(self, job_id):
        """Segmentation/tracking tools were removed pending agent redesign."""
        del job_id
        return {
            "error": "vision algorithms removed; agent workflow is being redesigned"
        }, 410


class StatusView(PropertyView):
    def get(self):
        """Visual prompt storage status on the microscope."""
        return self.extension.store.status()


class SettingsView(PropertyView):
    args = {
        "max_bytes": fields.Integer(
            required=False,
            metadata={"description": "Rolling storage budget in bytes (default 2GiB)"},
        )
    }

    def get(self):
        """Read visual prompt storage settings."""
        return self.extension.store.read_settings()

    def post(self, args):
        """Update visual prompt storage settings."""
        return self.extension.store.write_settings(args or {})

    def put(self, args):
        return self.post(args)


class PromptsView(View):
    def get(self):
        """List stored visual prompts (newest first)."""
        return {"items": self.extension.store.list_manifests()}

    def post(self):
        """Create a visual prompt from frame/ROI data URLs (also queues an agent job)."""
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            return self.extension.store.create(payload), 201
        except ValueError as exc:
            return {"error": str(exc)}, 400


class PromptItemView(View):
    def get(self, prompt_id):
        try:
            return self.extension.store.get_manifest(prompt_id)
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400


class PromptFileView(View):
    def get(self, prompt_id, kind):
        if kind not in {"frame", "roi", "prompt", "vp"}:
            return {"error": "kind must be frame, roi, prompt, or vp"}, 400
        try:
            path, _manifest = self.extension.store.file_for(prompt_id, kind)
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        if kind == "prompt":
            return Response(path.read_text(encoding="utf-8"), mimetype="text/markdown; charset=utf-8")
        return send_file(str(path), mimetype="image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png")


class LatestView(PropertyView):
    def get(self):
        """Metadata for the newest visual prompt."""
        manifest = self.extension.store.latest_manifest()
        if not manifest:
            return {"error": "no visual prompts yet"}, 404
        return {
            **manifest,
            "links": {
                "frame": "/api/v2/extensions/{}/latest/frame".format(EXTENSION_ID),
                "roi": "/api/v2/extensions/{}/latest/roi".format(EXTENSION_ID),
                "vp": "/api/v2/extensions/{}/latest/vp".format(EXTENSION_ID),
                "prompt": "/api/v2/extensions/{}/latest/prompt".format(EXTENSION_ID),
                "job": "/api/v2/extensions/{}/jobs/{}".format(
                    EXTENSION_ID, manifest.get("job_id") or manifest.get("id")
                ),
            },
        }


class LatestFileView(View):
    def get(self, kind):
        if kind not in {"frame", "roi", "prompt", "vp"}:
            return {"error": "kind must be frame, roi, prompt, or vp"}, 400
        try:
            path, _manifest = self.extension.store.file_for("latest", kind)
        except FileNotFoundError:
            return {"error": "no visual prompts yet"}, 404
        if kind == "prompt":
            return Response(path.read_text(encoding="utf-8"), mimetype="text/markdown; charset=utf-8")
        return send_file(str(path), mimetype="image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png")


class JobsPendingView(View):
    def get(self):
        """List queued agent jobs (claimable)."""
        items = self.extension.store.list_pending_jobs()
        return {"ok": True, "items": items, "count": len(items)}


class JobsLatestView(View):
    def get(self):
        """Newest agent job (any status), with result if present."""
        job = self.extension.store.latest_job()
        if not job:
            return {"error": "no jobs yet"}, 404
        return {"ok": True, "job": job}


class JobItemView(View):
    def get(self, job_id):
        try:
            return {"ok": True, "job": self.extension.store.get_job(job_id)}
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400


class JobClaimView(View):
    def post(self, job_id):
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            job = self.extension.store.claim_job(
                job_id,
                payload.get("worker_id"),
                lease_seconds=payload.get("lease_seconds"),
            )
            return {"ok": True, "job": job}
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        except ValueError as exc:
            return {"error": str(exc)}, 400


class JobHeartbeatView(View):
    def post(self, job_id):
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            job = self.extension.store.heartbeat_job(
                job_id,
                payload.get("worker_id"),
                lease_seconds=payload.get("lease_seconds"),
                progress=payload.get("progress"),
                progress_step=payload.get("progress_step"),
            )
            return {"ok": True, "job": job}
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except RuntimeError as exc:
            msg = str(exc)
            code = 409
            if "cancelled" in msg.lower():
                code = 409
            return {"error": msg, "cancelled": "cancelled" in msg.lower()}, code
        except ValueError as exc:
            return {"error": str(exc)}, 400


class JobCancelView(View):
    def post(self, job_id):
        """UI interrupt — mark job cancelled; worker stops on next heartbeat."""
        try:
            job = self.extension.store.cancel_job(job_id)
            return {"ok": True, "job": job}
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400


class JobResultView(View):
    def post(self, job_id):
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            job = self.extension.store.submit_job_result(
                job_id,
                payload,
                worker_id=payload.get("worker_id"),
            )
            return {"ok": True, "job": job}
        except FileNotFoundError:
            return {"error": "not found"}, 404
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        except ValueError as exc:
            return {"error": str(exc)}, 400


class SessionsView(View):
    def get(self):
        from flask import request

        view_id = str(request.args.get("view_id") or "").strip()
        if view_id:
            session = self.extension.store.find_session_by_view_id(view_id)
            return {"ok": True, "session": session, "latest": session}
        latest = self.extension.store.latest_session()
        return {"ok": True, "latest": latest}

    def post(self):
        from flask import request

        payload = request.get_json(silent=True) or {}
        session = self.extension.store.create_session(
            note=payload.get("note") or "",
            view_id=payload.get("view_id"),
        )
        return {"ok": True, "session": session}


class SessionsLatestView(View):
    def get(self):
        session = self.extension.store.latest_session()
        if not session:
            return {"ok": True, "session": None}
        return {"ok": True, "session": session}


class SessionItemView(View):
    def get(self, session_id):
        try:
            return {"ok": True, "session": self.extension.store.read_session(session_id)}
        except FileNotFoundError:
            return {"error": "not found"}, 404


class SessionMessagesView(View):
    def post(self, session_id):
        """Append a user turn: save prompt + queue job bound to session."""
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            out = self.extension.store.post_session_turn(session_id, payload)
            return {"ok": True, **out}
        except FileNotFoundError:
            return {"error": "session not found"}, 404
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except Exception as exc:
            logging.exception("session turn failed")
            return {"error": str(exc)}, 500


class SessionThreadView(View):
    """GET/POST slim LLM thread_items for multi-turn agent continuity."""

    def get(self, session_id):
        try:
            session = self.extension.store.read_session(session_id)
            return {
                "ok": True,
                "thread_items": session.get("thread_items") or [],
            }
        except FileNotFoundError:
            return {"error": "not found"}, 404

    def post(self, session_id):
        from flask import request

        payload = request.get_json(silent=True) or {}
        items = payload.get("thread_items")
        if items is None:
            items = payload.get("items") or []
        try:
            session = self.extension.store.set_session_thread_items(session_id, items)
            return {
                "ok": True,
                "count": len(session.get("thread_items") or []),
            }
        except FileNotFoundError:
            return {"error": "session not found"}, 404
        except Exception as exc:
            logging.exception("set thread_items failed")
            return {"error": str(exc)}, 500


class AnnotationsView(View):
    def get(self):
        return {"ok": True, "items": self.extension.store.list_annotations()}

    def post(self):
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            meta = self.extension.store.add_annotation(payload)
            return {"ok": True, "annotation": meta}
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except Exception as exc:
            logging.exception("add annotation failed")
            return {"error": str(exc)}, 500


class AnnotationsExportView(View):
    def post(self):
        from flask import request

        payload = request.get_json(silent=True) or {}
        try:
            info = self.extension.store.export_cellpose_dataset(
                split=payload.get("split") or "train",
                dest_name=payload.get("dest_name"),
            )
            return info
        except Exception as exc:
            logging.exception("export failed")
            return {"error": str(exc)}, 500


LABTHINGS_EXTENSIONS = (
    VisualPromptsExtension,
    legacy_extension(VisualPromptsExtension, EXTENSION_ID, LEGACY_EXTENSION_ID, EXTENSION_VERSION),
)

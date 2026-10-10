"""One editable project list of work-area domains and worker memberships, shared by the Human and Main.

Immutable execution evidence (allocation, taskBinding, receipts, run states) is never rewritten here: a task keeps
the domain name Main recorded when it was accepted. This store only names domains and links workers to them.
Human edits are protected: Main ("ai") may reuse any domain and create new ones, but may not rename a domain whose
current name the Human set, nor move a worker the Human placed. Every write is serialized by one project lock and
may carry the revision the writer read; a stale revision fails instead of overwriting a newer edit.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

from storage.errors import ContractError
from storage.files import AGENT_ID, atomic_write_json, file_lock, safe_read_json
from system.containment import now

SCHEMA_VERSION = 1
FILE = "domains.json"
LOCK = ".domains.lock"
ACTORS = ("human", "ai")
DOMAIN_ID = re.compile(r"^domain-[0-9a-f]{12}$")
HISTORY_LIMIT = 500
# The Human's "new domain" starts as a real domain with this provisional name, renamed afterwards. Provisional
# domains never match by name, so repeated creations stay independent and Main never reuses one by its name.
PLACEHOLDER_NAME = "미분류"


def empty():
    return {"schemaVersion": SCHEMA_VERSION, "kind": "project-domains", "revision": 0, "domains": [], "assignments": {},
            "removedWorkers": {}, "history": []}


def name_of(value):
    """Domain names follow allocation.domain: a trimmed single line of at most 80 characters."""
    if (not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 80
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ContractError("domain_name_invalid", "A domain name is a trimmed single line of at most 80 characters")
    return value


def key(value):
    return " ".join(value.split()).casefold()


def _actor(actor, source):
    if actor not in ACTORS:
        raise ContractError("domain_actor_invalid", "actor must be human or ai")
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ContractError("domain_source_invalid", "Record the evidence for this change (a run, loop or control-center action)")
    return {"actor": actor, "at": now(), "source": source}


def read(runtime_root: Path):
    path = Path(runtime_root) / FILE
    if not path.exists():
        return empty()
    value = safe_read_json(path)
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION or value.get("kind") != "project-domains":
        raise ContractError("domain_store_invalid", "Unsupported project domain store")
    value.setdefault("removedWorkers", {})  # Stores written before worker removal existed.
    return value


def find(document, name):
    """The domain whose current name or a former name matches; renamed domains keep earlier task names linked."""
    wanted = key(name)
    for domain in document["domains"]:
        if domain.get("provisional"):
            continue
        if key(domain["name"]) == wanted or any(key(alias) == wanted for alias in domain.get("aliases", [])):
            return domain
    return None


def _mutate(runtime_root, expected_revision, change):
    root = Path(runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / LOCK):
        document = read(root)
        if expected_revision is not None and expected_revision != document["revision"]:
            raise ContractError("domain_conflict", f"Domains changed since revision {expected_revision}; reload and retry")
        before = json.dumps(document, sort_keys=True)
        result = change(document)
        if json.dumps(document, sort_keys=True) != before:
            document["revision"] += 1
            for event in document["history"]:
                event.setdefault("revision", document["revision"])
            document["history"] = document["history"][-HISTORY_LIMIT:]
            atomic_write_json(root / FILE, document)
        return {**result, "revision": document["revision"], "domains": copy.deepcopy(document)}


def _event(document, by, action, **fields):
    document["history"].append({**by, "action": action, **fields})


def create(runtime_root, name, actor, source, expected_revision=None, placeholder=False):
    """Create a named domain, or (Human only) a provisional one named PLACEHOLDER_NAME to be renamed later."""
    if placeholder and (actor != "human" or name is not None):
        raise ContractError("domain_placeholder_invalid", "Only the Human creates a provisional domain, without a name")
    name, by = (PLACEHOLDER_NAME if placeholder else name_of(name)), _actor(actor, source)

    def change(document):
        if placeholder:
            domain = {"id": "domain-" + uuid.uuid4().hex[:12], "name": name, "aliases": [], "provisional": True, "createdBy": by, "nameSetBy": by}
            document["domains"].append(domain)
            _event(document, by, "create", domainId=domain["id"], name=name, provisional=True)
            return {"domainId": domain["id"], "reused": False}
        existing = find(document, name)
        if existing:
            if actor == "human":
                raise ContractError("domain_name_taken", f"A domain named {existing['name']!r} already exists")
            return {"domainId": existing["id"], "reused": True}
        domain = {"id": "domain-" + uuid.uuid4().hex[:12], "name": name, "aliases": [], "createdBy": by, "nameSetBy": by}
        document["domains"].append(domain)
        _event(document, by, "create", domainId=domain["id"], name=name)
        return {"domainId": domain["id"], "reused": False}
    return _mutate(runtime_root, expected_revision, change)


def rename(runtime_root, domain_id, name, actor, source, expected_revision=None):
    name, by = name_of(name), _actor(actor, source)

    def change(document):
        domain = next((item for item in document["domains"] if item["id"] == domain_id), None)
        if domain is None:
            raise ContractError("domain_missing", "Unknown domain")
        if actor == "ai" and domain["nameSetBy"]["actor"] == "human":
            raise ContractError("domain_protected", "The Human named this domain; only the Human renames it")
        other = find(document, name)
        if other and other["id"] != domain_id:
            raise ContractError("domain_name_taken", f"Another domain is already named {other['name']!r}; domains are not merged")
        provisional = bool(domain.pop("provisional", False))
        if domain["name"] == name and not provisional:
            return {"domainId": domain_id}
        # A provisional name is not a former name: it would link every other unnamed domain's tasks.
        if not provisional and key(domain["name"]) not in {key(alias) for alias in domain["aliases"]}:
            domain["aliases"].append(domain["name"])
        domain["aliases"] = [alias for alias in domain["aliases"] if key(alias) != key(name)]
        _event(document, by, "rename", domainId=domain_id, previous=domain["name"], name=name)
        domain["name"], domain["nameSetBy"] = name, by
        return {"domainId": domain_id}
    return _mutate(runtime_root, expected_revision, change)


def assign(runtime_root, agent_id, domain_id, actor, source, expected_revision=None):
    """Place a worker in a domain (None: unclassified). Main cannot move a worker the Human placed.

    A Human "unclassified" choice is kept as a protected entry so a later dispatch does not re-place the worker.
    """
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise ContractError("domain_agent_invalid", "Invalid worker agent ID")
    by = _actor(actor, source)

    def change(document):
        if domain_id is not None and not any(item["id"] == domain_id for item in document["domains"]):
            raise ContractError("domain_missing", "Unknown domain")
        current = document["assignments"].get(agent_id)
        if actor == "ai" and current and current["setBy"]["actor"] == "human":
            raise ContractError("domain_protected", "The Human placed this worker; only the Human moves it")
        if current is None and domain_id is None and actor == "ai":
            return {"agentId": agent_id, "domainId": None}
        if current and current["domainId"] == domain_id and (actor == "ai" or current["setBy"]["actor"] == "human"):
            return {"agentId": agent_id, "domainId": domain_id}
        if domain_id is None and actor == "ai":
            document["assignments"].pop(agent_id, None)
        else:
            document["assignments"][agent_id] = {"domainId": domain_id, "setBy": by}
        _event(document, by, "assign", agentId=agent_id, domainId=domain_id, previous=(current or {}).get("domainId"))
        return {"agentId": agent_id, "domainId": domain_id}
    return _mutate(runtime_root, expected_revision, change)


def link_dispatch(runtime_root, agent_id, name, source):
    """Main's accepted allocation domain: reuse a matching domain or create it, then place an unplaced worker.

    A worker the Human placed stays where the Human put it; the accepted allocation itself is not changed.
    """
    created = create(runtime_root, name, "ai", source)
    try:
        placed = assign(runtime_root, agent_id, created["domainId"], "ai", source)
        return {"domainId": created["domainId"], "reused": created["reused"], "assigned": True, "revision": placed["revision"]}
    except ContractError as error:
        if error.code != "domain_protected":
            raise
        current = read(runtime_root)["assignments"].get(agent_id, {})
        return {"domainId": created["domainId"], "reused": created["reused"], "assigned": False,
                "protectedDomainId": current.get("domainId"), "revision": read(runtime_root)["revision"]}


ACTIVE_RUN_STATES = {"accepted", "queued", "starting", "running", "cancelling"}


def _worker_active(runtime_root, agent_id):
    """A worker with a live run or an active loop must be stopped first; its records are only read."""
    agent = Path(runtime_root) / "agents" / agent_id
    for state_path in list(agent.glob("runs/*/state.json")) + list(agent.glob("loops/*/state.json")):
        try:
            state = safe_read_json(state_path)
        except (ContractError, OSError, ValueError):
            continue
        status = state.get("status")
        if state_path.parent.parent.name == "loops" and status == "active":
            return True
        if state_path.parent.parent.name == "runs" and status in ACTIVE_RUN_STATES:
            return True
    return False


def remove_worker(runtime_root, agent_id, actor, source, expected_revision=None):
    """Hide a worker from the worker list (Human only). Its sessions, runs, results and loops are untouched."""
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise ContractError("domain_agent_invalid", "Invalid worker agent ID")
    if actor != "human":
        raise ContractError("domain_protected", "Only the Human removes a worker from the list")
    by = _actor(actor, source)

    def change(document):
        if agent_id in document["removedWorkers"]:
            return {"agentId": agent_id, "removed": True}
        # Checked under the domain lock; the run/loop records themselves are owned by the runtime.
        if _worker_active(runtime_root, agent_id):
            raise ContractError("worker_running", "Stop this worker's running task before removing it")
        document["removedWorkers"][agent_id] = {"removedBy": by}
        _event(document, by, "remove-worker", agentId=agent_id)
        return {"agentId": agent_id, "removed": True}
    return _mutate(runtime_root, expected_revision, change)

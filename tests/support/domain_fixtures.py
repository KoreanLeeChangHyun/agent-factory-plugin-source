"""Loop fixtures dispatch known workers; every worker belongs to a real domain, so Main's link is made up front."""
from pathlib import Path

# Worker IDs the loop fixtures dispatch; tests about domain membership itself use their own IDs.
FIXTURE_WORKERS = ("work-agent", "verification-agent", "work", "verify", "one-worker", "work-other", "work-human",
                   "work-main", "read-only-agent", "code-agent", "scribe-code-agent", "scribe-shared-agent")


def place_workers(project_root, agents=FIXTURE_WORKERS, name="fixture work area"):
    from storage import paths as runtime_paths
    from conductor import domains
    runtime_root = Path(runtime_paths.resolve(Path(project_root), create=True)["runtimeRoot"])
    for agent in agents:
        domains.link_dispatch(runtime_root, agent, name, "test fixture")
    return runtime_root

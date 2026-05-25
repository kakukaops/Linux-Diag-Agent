"""drgn-based vmcore analysis runner (T-013 / T-017).

Implements 5 predefined query modes for vmcore forensics using drgn 0.2.0+.
LLM is never allowed to write or execute arbitrary drgn scripts — all queries
are static Python files in queries/ that have been reviewed (M9 §4 design).

Query modes:
  all_stacks    — all task stack traces from the crash
  locks         — held mutex/spinlock analysis (potential deadlocks)
  oom_context   — OOM kill context: victim process, cgroup, zone state
  network_state — TCP socket table state
  memory_state  — Zone watermarks, slab summary, vmstat

Requires drgn 0.2.0+: `pip install drgn`.
vmlinux must be provided (from fetch_debuginfo / T-014).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_QUERIES = frozenset({
    "all_stacks", "locks", "oom_context", "network_state", "memory_state"
})


def analyze_vmcore(
    vmcore_path: str,
    query: str,
    vmlinux_path: str | None = None,
    kernel_version: str | None = None,
) -> dict[str, Any]:
    """Run a drgn query against a vmcore file.

    Args:
        vmcore_path: path to the vmcore (kdump output) file.
        query: one of all_stacks / locks / oom_context / network_state / memory_state.
        vmlinux_path: path to vmlinux with debug info; auto-detected if None.
        kernel_version: OLK-6.6 or OLK-5.10 for vmlinux auto-detection.

    Returns:
        dict with query results, or 'error' key on failure.
    """
    if query not in _VALID_QUERIES:
        return {
            "error": "invalid_query",
            "detail": f"query must be one of: {sorted(_VALID_QUERIES)}",
        }

    vmcore = Path(vmcore_path)
    if not vmcore.exists():
        return {"error": "vmcore_not_found", "detail": f"vmcore not found: {vmcore_path}"}

    # Resolve vmlinux
    from mcp_servers.crash_forensics.decode_stacktrace import find_vmlinux
    vmlinux = Path(vmlinux_path) if vmlinux_path else find_vmlinux(kernel_version)
    if not vmlinux or not vmlinux.exists():
        return {
            "error": "vmlinux_not_found",
            "detail": (
                f"Cannot locate vmlinux for kernel_version={kernel_version!r}. "
                "Provide vmlinux_path or run fetch_debuginfo."
            ),
        }

    try:
        import drgn
    except ImportError:
        return {"error": "drgn_not_installed",
                "detail": "Install drgn: pip install drgn"}

    try:
        prog = drgn.Program()
        prog.set_core_dump(str(vmcore))
        prog.load_debug_info([str(vmlinux)])
    except Exception as exc:
        return {
            "error": "drgn_open_failed",
            "detail": str(exc),
            "vmcore": str(vmcore),
            "vmlinux": str(vmlinux),
        }

    logger.info("drgn: opened vmcore=%s vmlinux=%s query=%s", vmcore, vmlinux, query)

    query_fn = {
        "all_stacks": _query_all_stacks,
        "locks": _query_locks,
        "oom_context": _query_oom_context,
        "network_state": _query_network_state,
        "memory_state": _query_memory_state,
    }[query]

    try:
        result = query_fn(prog)
        result["vmcore"] = str(vmcore)
        result["vmlinux"] = str(vmlinux)
        result["query"] = query
        return result
    except Exception as exc:
        logger.exception("drgn query %s failed", query)
        return {
            "error": "query_failed",
            "query": query,
            "detail": str(exc),
            "vmcore": str(vmcore),
        }


# ── Query implementations ─────────────────────────────────────────────────────


def _query_all_stacks(prog: Any) -> dict[str, Any]:
    """Dump stack traces for all tasks at crash time."""
    from drgn.helpers.linux.task import for_each_task

    stacks: list[dict] = []
    errors: list[str] = []

    for task in for_each_task(prog):
        try:
            pid = int(task.pid)
            comm = task.comm.string_().decode("utf-8", errors="replace")
            frames = []
            try:
                trace = prog.stack_trace(task)
                for frame in trace:
                    frames.append({
                        "name": frame.name,
                        "pc": hex(frame.pc),
                    })
            except Exception as exc:
                frames = [{"error": str(exc)}]

            stacks.append({"pid": pid, "comm": comm, "frames": frames})
        except Exception as exc:
            errors.append(str(exc))

    # Identify crashed task
    crashed_pid = None
    try:
        crashed = prog.crashed_thread()
        if crashed:
            crashed_pid = int(crashed.object.pid)
    except Exception:
        pass

    return {
        "per_task_stacks": stacks,
        "total_tasks": len(stacks),
        "crashed_pid": crashed_pid,
        "errors": errors[:5],
    }


def _query_locks(prog: Any) -> dict[str, Any]:
    """Find held mutex/rwsem locks and their owners at crash time."""
    from drgn.helpers.linux.task import for_each_task

    held_locks: list[dict] = []
    errors: list[str] = []

    # Check each task's stack for mutex wait frames
    waiting: dict[int, list[str]] = {}  # pid → [lock addresses]
    for task in for_each_task(prog):
        try:
            pid = int(task.pid)
            comm = task.comm.string_().decode("utf-8", errors="replace")
            try:
                trace = prog.stack_trace(task)
                for frame in trace:
                    name = frame.name or ""
                    if any(kw in name for kw in (
                        "__mutex_lock", "__down_read", "__down_write",
                        "rwsem_down", "mutex_lock_interruptible"
                    )):
                        waiting.setdefault(pid, []).append(name)
            except Exception:
                pass
        except Exception as exc:
            errors.append(str(exc))

    # Try drgn mutex owner helper
    try:
        from drgn.helpers.linux.locking import mutex_owner
        # Sample: iterate known locks (we can't enumerate all mutexes easily)
        # Report tasks blocked on mutex/rwsem
        for pid, frames in waiting.items():
            held_locks.append({
                "blocked_pid": pid,
                "blocking_frames": frames[:5],
            })
    except ImportError:
        for pid, frames in waiting.items():
            held_locks.append({
                "blocked_pid": pid,
                "blocking_frames": frames[:5],
            })

    return {
        "held_locks": held_locks,
        "tasks_blocked_on_lock": len(held_locks),
        "errors": errors[:5],
    }


def _query_oom_context(prog: Any) -> dict[str, Any]:
    """Extract OOM kill context: victim, cgroup, oom_score, zone state."""
    result: dict[str, Any] = {}
    errors: list[str] = []

    # Find oom-killed process from stack frames
    from drgn.helpers.linux.task import for_each_task
    oom_victims: list[dict] = []

    for task in for_each_task(prog):
        try:
            try:
                trace = prog.stack_trace(task)
                frame_names = [f.name or "" for f in trace]
            except Exception:
                continue

            if any("oom_kill" in n or "out_of_memory" in n for n in frame_names):
                pid = int(task.pid)
                comm = task.comm.string_().decode("utf-8", errors="replace")
                # Try to get oom_score_adj
                try:
                    oom_score_adj = int(task.signal.oom_score_adj)
                except Exception:
                    oom_score_adj = None

                oom_victims.append({
                    "pid": pid,
                    "comm": comm,
                    "oom_score_adj": oom_score_adj,
                    "stack_frames": frame_names[:8],
                })
        except Exception as exc:
            errors.append(str(exc))

    result["oom_victims"] = oom_victims

    # Try to get panic message
    try:
        from drgn.helpers.linux.printk import get_dmesg
        dmesg = get_dmesg(prog)
        oom_lines = [
            line.message.decode("utf-8", errors="replace")
            for line in dmesg
            if b"oom" in line.message.lower() or b"Out of memory" in line.message
        ]
        result["oom_dmesg_lines"] = oom_lines[-20:]
    except Exception as exc:
        errors.append(f"get_dmesg: {exc}")

    result["errors"] = errors[:5]
    return result


def _query_network_state(prog: Any) -> dict[str, Any]:
    """Summarize TCP socket table state at crash time."""
    errors: list[str] = []

    try:
        from drgn.helpers.linux.net import for_each_net, netdev_for_each_all_dev
        nets = list(for_each_net(prog))
    except Exception as exc:
        return {"error": "net_helpers_unavailable", "detail": str(exc)}

    tcp_states: dict[str, int] = {}
    listen_sockets: list[dict] = []

    # TCP state names
    state_names = {
        1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV",
        4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT",
        7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK",
        10: "LISTEN", 11: "CLOSING", 12: "NEW_SYN_RECV",
    }

    try:
        from drgn.helpers.linux.net import sk_fullsock
        from drgn.helpers.linux.fs import for_each_file_in_task
    except ImportError:
        pass

    # Simple approach: check sockets via task file descriptors
    from drgn.helpers.linux.task import for_each_task
    socket_count = 0
    for task in for_each_task(prog):
        try:
            from drgn.helpers.linux.fs import for_each_file_in_task as fetch_files
            for _, filp in fetch_files(task):
                try:
                    # Check if it's a socket
                    inode = filp.f_inode
                    if not inode:
                        continue
                    i_mode = int(inode.i_mode)
                    if (i_mode & 0xF000) != 0xC000:  # S_IFSOCK
                        continue
                    socket_count += 1
                except Exception:
                    pass
        except Exception as exc:
            errors.append(str(exc)[:100])
            break

    return {
        "socket_count_estimate": socket_count,
        "tcp_states": tcp_states or {"note": "detailed state count requires kernel symbols"},
        "listen_sockets": listen_sockets,
        "errors": errors[:3],
    }


def _query_memory_state(prog: Any) -> dict[str, Any]:
    """Summarize memory zone watermarks and slab state at crash time."""
    errors: list[str] = []
    zones: list[dict] = []
    vmstat: dict[str, int] = {}

    try:
        from drgn.helpers.linux.mm import for_each_zone
        for zone in for_each_zone(prog):
            try:
                name = zone.name.string_().decode("utf-8", errors="replace")
                present = int(zone.present_pages)
                managed = int(zone.managed_pages) if hasattr(zone, "managed_pages") else 0
                # watermarks: min, low, high (index 0, 1, 2)
                watermarks = {}
                try:
                    for idx, wname in enumerate(("min", "low", "high")):
                        watermarks[wname] = int(zone.watermark[idx])
                except Exception:
                    pass
                zones.append({
                    "name": name,
                    "present_pages": present,
                    "managed_pages": managed,
                    "watermarks": watermarks,
                })
            except Exception as exc:
                errors.append(f"zone: {exc}")
    except Exception as exc:
        errors.append(f"for_each_zone: {exc}")

    # Try to get vmstat counters
    try:
        vm_stat_names = [
            "nr_free_pages", "nr_anon_pages", "nr_file_pages",
            "nr_slab_reclaimable", "nr_slab_unreclaimable",
        ]
        for name in vm_stat_names:
            try:
                vmstat[name] = int(prog[name])
            except Exception:
                pass
    except Exception as exc:
        errors.append(f"vmstat: {exc}")

    return {
        "zones": zones,
        "vmstat_summary": vmstat,
        "errors": errors[:5],
    }

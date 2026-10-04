"""Process-tree helpers for validation, including Windows venv launcher children."""
import psutil


def process_tree(pid):
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def tree_usage(pid):
    rss, cpu = 0, 0
    for process in process_tree(pid):
        try:
            rss += process.memory_info().rss
            cpu += sum(process.cpu_times()[:2])
        except psutil.NoSuchProcess:
            pass
    return rss, cpu


def stop_tree(process):
    # Only descendants of this explicitly launched subprocess are targeted.
    children = process_tree(process.pid)
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(children, timeout=10)
    process.wait(timeout=10)

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import socket
import ipaddress


def lan_addresses():
    result = {"127.0.0.1", "localhost"}
    try:
        result.update(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(result)


@dataclass
class Config:
    storage: Path = field(default_factory=lambda: Path("storage").absolute())
    hosts: list[str] = field(default_factory=lan_addresses)
    origins: list[str] = field(default_factory=list)
    port: int = 8000
    bind: str = "0.0.0.0"
    secure: bool = False
    chunk_size: int = 16 * 1024 * 1024
    active_files: int = 2
    safety_bytes: int = 1024 * 1024 * 1024
    max_jobs: int = 100
    max_jobs_per_session: int = 20
    max_entries_per_job: int = 100000
    max_entries_per_session: int = 200000
    session_days: int = 30
    download_idle_seconds: float = 30
    allowed_networks: list[str] = field(default_factory=lambda: [
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
    ])
    share_roots: list[str] = field(default_factory=list)
    frontend: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "frontend" / "dist")

    def __post_init__(self):
        self.storage = Path(self.storage).absolute()
        self.frontend = Path(self.frontend).absolute()
        if self.chunk_size not in [n * 1024 * 1024 for n in (4, 8, 16, 32)]:
            raise ValueError("chunk_size must be 4, 8, 16, or 32 MiB")
        if self.active_files not in (1, 2, 4) or self.safety_bytes < 0:
            raise ValueError("Invalid concurrency or safety margin")
        if any(n <= 0 for n in (self.max_jobs, self.max_jobs_per_session, self.max_entries_per_job,
                                 self.max_entries_per_session, self.session_days, self.download_idle_seconds)):
            raise ValueError("Limits, session lifetime and download timeout must be positive")
        for network in self.allowed_networks:
            ipaddress.ip_network(network)
        if not 1 <= self.port <= 65535 or not self.hosts or not self.allowed_networks:
            raise ValueError('Use a valid port, at least one server host and at least one allowed client network')
        if any("*" in host for host in self.hosts + self.origins):
            raise ValueError("Exact hosts and origins required; wildcards are not allowed")
        if not self.origins:
            scheme = "https" if self.secure else "http"
            self.origins = [f"{scheme}://{host}:{self.port}" for host in self.hosts]

    @classmethod
    def load(cls, path=None):
        path = path or os.environ.get("LAN_TRANSFER_CONFIG", "config.json")
        path = Path(path).absolute()
        if not path.exists():
            return cls(storage=path.parent / 'storage')
        data = json.loads(path.read_text('utf-8'))
        for key in ('storage', 'frontend'):
            if key in data and not Path(data[key]).is_absolute():
                data[key] = str(path.parent / data[key])
        if 'share_roots' in data:
            data['share_roots'] = [str(Path(root) if Path(root).is_absolute() else path.parent / root) for root in data['share_roots']]
        return cls(**data)

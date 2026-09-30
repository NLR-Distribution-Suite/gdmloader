import json
import os
import shutil
from typing import Any, Type
from pathlib import Path
from typing_extensions import Annotated
import importlib.metadata

from infrasys.system import System
from pydantic import BaseModel, Field, PrivateAttr
import fsspec
from rich.console import Console
from rich.table import Table


class SourceModel(BaseModel):
    """Backend-agnostic dataset source. Filesystem is created lazily."""

    name: Annotated[str, Field(..., description="Name of the data source")]
    url: Annotated[str | None, Field(default=None, description="URL of the data source")]
    folder: Annotated[str, Field(..., description="Entry folder of the data source")]
    protocol: Annotated[
        str, Field(default="gcs", description='fsspec protocol, e.g. "gcs", "github", "file", "s3"')
    ]
    storage_options: Annotated[
        dict[str, Any], Field(default_factory=dict, description="Options passed to fsspec.filesystem")
    ]
    path_prefix: Annotated[
        str,
        Field(
            default="",
            description="Prefix prepended to remote paths (e.g. GCS bucket name). Empty for backends already scoped (e.g. github).",
        ),
    ]

    _fs_cache: Any = PrivateAttr(default=None)
    _fs_override: Any = PrivateAttr(default=None)

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, fs: Any | None = None, **data: Any):
        # Back-compat: allow SourceModel(fs=..., ...) without network at init.
        super().__init__(**data)
        self._fs_override = fs

    @property
    def fs(self) -> fsspec.AbstractFileSystem:
        """Lazily-created filesystem. No network I/O happens at import/init."""
        if self._fs_override is not None:
            return self._fs_override
        if self._fs_cache is None:
            self._fs_cache = fsspec.filesystem(self.protocol, **self.storage_options)
        return self._fs_cache

    def remote_path(self, *parts: str) -> str:
        absolute = self.path_prefix.startswith("/")
        cleaned = [p.strip("/") for p in parts if p and p.strip("/")]
        base = [self.path_prefix.strip("/"), self.folder.strip("/")]
        full = [p for p in base + cleaned if p]
        joined = "/".join(full)
        return "/" + joined if absolute else joined


def get_gcs_source(
    bucket: str = "gdm_data",
    folder: str = "data",
    name: str = "gdm_data",
    url: str | None = "https://storage.googleapis.com/gdm_data",
    project: str | None = None,
    token: str | None = None,
    **kwargs: Any,
) -> SourceModel:
    """GCS source using Application Default Credentials (works on GCE/GKE/Cloud Run with no token)."""
    storage_options: dict[str, Any] = dict(kwargs)
    if project is not None:
        storage_options["project"] = project
    if token is not None:
        storage_options["token"] = token
    return SourceModel(
        name=name, url=url, folder=folder, protocol="gcs",
        storage_options=storage_options, path_prefix=bucket,
    )


def get_github_source(
    org: str = "NREL-Distribution-Suites",
    repo: str = "gdm-cases",
    branch: str = "main",
    folder: str = "data",
    name: str = "gdm-cases",
    url: str | None = None,
    username: str | None = None,
    token: str | None = None,
) -> SourceModel:
    """GitHub source. Uses sha=branch to skip the default_branch API lookup."""
    username = username or os.environ.get("GITHUB_USERNAME") or os.environ.get("GH_USERNAME")
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token is not None and username is None:
        username = "oauth"  # GitHub accepts any username when authing with a PAT
    storage_options: dict[str, Any] = {"org": org, "repo": repo, "sha": branch}
    if username is not None and token is not None:
        storage_options.update({"username": username, "token": token})
    return SourceModel(
        name=name,
        url=url or f"https://github.com/{org}/{repo}",
        folder=folder,
        protocol="github",
        storage_options=storage_options,
        path_prefix="",  # github fs is already scoped to the repo
    )


def get_local_source(
    base_dir: str | Path,
    folder: str = "data",
    name: str = "local",
    url: str | None = None,
) -> SourceModel:
    base = str(base_dir).rstrip("/")
    return SourceModel(
        name=name, url=url, folder=folder, protocol="file",
        storage_options={}, path_prefix=base,
    )


def resolve_source_from_env() -> SourceModel:
    """Pick a source from env so the same code runs on GCS now and anywhere later.

    GDMLOADER_BACKEND: "gcs" (default) | "github" | "file"
    GCS: GDMLOADER_BUCKET, GDMLOADER_FOLDER, GDMLOADER_GCS_PROJECT, GDMLOADER_GCS_TOKEN
    GitHub: GDMLOADER_GITHUB_ORG/REPO/BRANCH + GITHUB_USERNAME/GITHUB_TOKEN
    File: GDMLOADER_LOCAL_DIR + GDMLOADER_FOLDER
    """
    backend = os.environ.get("GDMLOADER_BACKEND", "gcs").lower()
    folder = os.environ.get("GDMLOADER_FOLDER", "data")
    if backend == "github":
        return get_github_source(
            org=os.environ.get("GDMLOADER_GITHUB_ORG", "NREL-Distribution-Suites"),
            repo=os.environ.get("GDMLOADER_GITHUB_REPO", "gdm-cases"),
            branch=os.environ.get("GDMLOADER_GITHUB_BRANCH", "main"),
            folder=folder,
        )
    if backend == "file":
        return get_local_source(
            base_dir=os.environ.get("GDMLOADER_LOCAL_DIR", "."),
            folder=folder,
            name=os.environ.get("GDMLOADER_NAME", "local"),
        )
    return get_gcs_source(
        bucket=os.environ.get("GDMLOADER_BUCKET", "gdm_data"),
        folder=folder,
        name=os.environ.get("GDMLOADER_NAME", "gdm_data"),
        project=os.environ.get("GDMLOADER_GCS_PROJECT"),
        token=os.environ.get("GDMLOADER_GCS_TOKEN"),
    )


def get_gdm_version() -> str:
    return importlib.metadata.version("grid-data-models").replace(".", "_")

def fix_version(version):
    if version.count(".") >= 3:
        return version.rsplit(".", 1)[0]
    return version


def _ls_names(fs: Any, path: str) -> list[str]:
    """Normalize fsspec ls across backends (github returns strs, gcs/memory return dicts)."""
    try:
        entries = fs.ls(path, detail=False)
    except TypeError:
        entries = fs.ls(path)
    names: list[str] = []
    for e in entries:
        if isinstance(e, dict):
            names.append(e.get("name", ""))
        else:
            names.append(str(e))
    return names


class SystemLoader:

    __doc_file_name__ = "doc.json"

    def __init__(self, cached_dir: Path = Path.home() / "gdmloader-cache"):
        self._sources: dict[str, SourceModel] = {}
        self._cached_folder = cached_dir
        if not self._cached_folder.exists():
            self._cached_folder.mkdir(parents=True)

    def show_sources(self):
        table = Table()
        table.add_column("Name", justify="right", style="cyan", no_wrap=True)
        table.add_column("URL", style="magenta")
        for _, source in self._sources.items():
            table.add_row(source.name, source.url)
        console = Console()
        console.print(table)

    def load_system_doc(self, system_name: str, source_name: str):
        local_doc_file_path = (
            self._cached_folder
            / source_name
            / system_name
            / self.__doc_file_name__
        )
        if not local_doc_file_path.exists():
            local_doc_file_path.parent.mkdir(parents=True, exist_ok=True)
            source = self._sources[source_name]
            remote_doc_file_path = source.remote_path(system_name, self.__doc_file_name__)
            source.fs.get(remote_doc_file_path, str(local_doc_file_path))
        with open(local_doc_file_path, "r", encoding="utf-8") as fpointer:
            contents = json.load(fpointer)
        return contents

    def show_dataset_by_system(self, system_name: str, source_name: str):
        table = Table(title=f"System: {system_name}")
        doc_contents = self.load_system_doc(system_name, source_name)
        for key in doc_contents[0]:
            table.add_column(key, justify="right", no_wrap=False)
        for item in doc_contents:
            table.add_row(*[str(k) for k in item.values()])
        console = Console()
        console.print(table)

        table = Table(title=f"System: {system_name} versions")
        table.add_column("Version", justify="right", no_wrap=True)
        source = self._sources[source_name]
        for version in _ls_names(source.fs, source.remote_path(system_name)):
            if version.endswith(".json"):
                continue
            version = Path(version).stem
            table.add_row(version)
        console.print(table)

    def show_dataset_by_source(self, source_name: str):
        source = self._sources[source_name]
        dir_path = source.remote_path()
        for system_folder in _ls_names(source.fs, dir_path):
            system_name = Path(system_folder).stem
            self.show_dataset_by_system(system_name, source_name)

    def load_dataset(
        self,
        system_type: Type[System],
        source_name: str,
        dataset_name: str,
        version: str | None = None,
    ):
        if version is None:
            version = fix_version(get_gdm_version())
        source = self._sources.get(source_name)
        if source is None:
            raise ValueError(f"Source {source_name} not found")
        remote_folder = source.remote_path(system_type.__name__, version, dataset_name)
        local_folder = self._cached_folder / source.name / system_type.__name__ / version
        if not local_folder.exists():
            local_folder.mkdir(parents=True)

        dataset_folder = local_folder / dataset_name
        if not dataset_folder.exists():
            try:
                source.fs.get(
                    remote_folder,
                    str(local_folder),
                    recursive=True,
                )
            except FileNotFoundError:
                msg = f"{remote_folder=} not found! Check the URL: {source.url}/{remote_folder}"
                raise ValueError(msg)

        system_file = list(dataset_folder.rglob("*.json"))[0]
        return system_type.from_json(system_file)

    def invalidate_cache(self):
        if self._cached_folder.exists():
            shutil.rmtree(self._cached_folder)

    def add_source(self, source: SourceModel):
        if source.name in self._sources:
            raise ValueError(f"Source {source.name} already exists")
        self._sources[source.name] = source

    def remove_source(self, source_name: str):
        if source_name not in self._sources:
            raise ValueError(f"Source {source_name} not found")
        self._sources.pop(source_name)

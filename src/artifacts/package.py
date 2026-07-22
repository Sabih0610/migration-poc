"""Secure deterministic artifact-package writer and reader."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from src.artifacts.schema_validation import validate_generated_artifact
from src.models.schemas import (
    ArtifactManifest,
    DeployableTargetType,
    GeneratedArtifact,
    GeneratedArtifactPackage,
    ManifestEntry,
)


class ArtifactPackageError(ValueError):
    """Raised for invalid definitions, unsafe paths, or digest mismatches."""


_DIRECTORY_FOR_TYPE = {
    DeployableTargetType.CONNECTION: "connections",
    DeployableTargetType.LAKEHOUSE: "lakehouses",
    DeployableTargetType.LAKEHOUSE_TABLE: "lakehouses",
    DeployableTargetType.DATAFLOW_GEN2: "dataflows",
    DeployableTargetType.DATA_PIPELINE: "pipelines",
    DeployableTargetType.SCHEDULE: "schedules",
}
_PACKAGE_DIRECTORIES = (
    "connections",
    "lakehouses",
    "dataflows",
    "pipelines",
    "schedules",
    "manifests",
)


def canonical_json(value: Any) -> str:
    """Return stable UTF-8 JSON text with no environment metadata."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_artifact_digest(artifact: GeneratedArtifact) -> str:
    payload = artifact.model_dump(mode="json", exclude={"content_digest"})
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def safe_filename(value: str) -> str:
    """Convert an artifact name to a deterministic path-safe stem."""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    normalized = normalized.replace("..", "_").strip("._-")
    if not normalized:
        normalized = "artifact"
    return normalized[:120]


def _relative_path(artifact: GeneratedArtifact, package_id: str) -> str:
    directory = _DIRECTORY_FOR_TYPE[artifact.target_type]
    filename = (
        f"{safe_filename(package_id)}--{safe_filename(artifact.target_name)}--"
        f"{artifact.content_digest[:12]}.json"
    )
    return f"{directory}/{filename}"


def _manifest_digest(entries: Iterable[ManifestEntry]) -> str:
    payload = [
        entry.model_dump(mode="json", exclude={"relative_path"})
        for entry in sorted(entries, key=lambda item: item.artifact_id)
    ]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_package(
    artifacts: Iterable[GeneratedArtifact],
) -> GeneratedArtifactPackage:
    """Finalize artifact/manifest digests and return a deterministic package."""
    finalized: list[GeneratedArtifact] = []
    for artifact in sorted(artifacts, key=lambda item: item.artifact_id):
        provisional = artifact.model_copy(update={"content_digest": ""})
        finalized.append(
            provisional.model_copy(
                update={"content_digest": compute_artifact_digest(provisional)}
            )
        )

    seed_entries = [
        ManifestEntry(
            artifact_id=artifact.artifact_id,
            target_type=artifact.target_type,
            target_name=artifact.target_name,
            relative_path="",
            content_digest=artifact.content_digest,
            dependencies=sorted(artifact.dependencies),
        )
        for artifact in finalized
    ]
    package_digest = _manifest_digest(seed_entries)
    package_id = f"package-{package_digest[:20]}"
    entries = [
        entry.model_copy(
            update={
                "relative_path": _relative_path(artifact, package_id)
            }
        )
        for entry, artifact in zip(seed_entries, finalized)
    ]
    manifest = ArtifactManifest(
        package_id=package_id,
        entries=entries,
        package_digest=package_digest,
    )
    return GeneratedArtifactPackage(
        package_id=package_id,
        artifacts=finalized,
        manifest=manifest,
    )


def _safe_join(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ArtifactPackageError("absolute package paths are forbidden")
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactPackageError("package path escapes output directory") from exc
    return candidate


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_package(package: GeneratedArtifactPackage, output_dir: Path) -> Path:
    """Atomically write and verify a package below ``output_dir``."""
    root = Path(output_dir).resolve()
    for directory in _PACKAGE_DIRECTORIES:
        _safe_join(root, directory).mkdir(parents=True, exist_ok=True)

    if package.package_id != package.manifest.package_id:
        raise ArtifactPackageError("package and manifest IDs do not match")
    if _manifest_digest(package.manifest.entries) != package.manifest.package_digest:
        raise ArtifactPackageError("manifest digest mismatch")

    artifacts = {artifact.artifact_id: artifact for artifact in package.artifacts}
    if set(artifacts) != {entry.artifact_id for entry in package.manifest.entries}:
        raise ArtifactPackageError("manifest entries do not match package artifacts")

    for entry in package.manifest.entries:
        artifact = artifacts[entry.artifact_id]
        if compute_artifact_digest(artifact) != artifact.content_digest:
            raise ArtifactPackageError(
                f"artifact digest mismatch: {artifact.artifact_id}"
            )
        if artifact.content_digest != entry.content_digest:
            raise ArtifactPackageError(
                f"manifest artifact digest mismatch: {artifact.artifact_id}"
            )
        schema = validate_generated_artifact(artifact)
        if not schema.valid:
            raise ArtifactPackageError(
                f"invalid generated definition {artifact.artifact_id}: "
                + "; ".join(schema.errors)
            )
        _atomic_write(
            _safe_join(root, entry.relative_path),
            canonical_json(artifact.model_dump(mode="json")),
        )

    manifest_path = _safe_join(
        root, f"manifests/{safe_filename(package.package_id)}.json"
    )
    _atomic_write(
        manifest_path, canonical_json(package.manifest.model_dump(mode="json"))
    )
    return manifest_path


def read_package(output_dir: Path, manifest_path: str | Path) -> GeneratedArtifactPackage:
    """Read a package with path, schema, and digest verification."""
    root = Path(output_dir).resolve()
    manifest_file = _safe_join(root, str(manifest_path))
    try:
        manifest = ArtifactManifest.model_validate_json(
            manifest_file.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ArtifactPackageError(f"invalid package manifest: {exc}") from exc
    if _manifest_digest(manifest.entries) != manifest.package_digest:
        raise ArtifactPackageError("manifest digest mismatch")
    expected_package_id = f"package-{manifest.package_digest[:20]}"
    if manifest.package_id != expected_package_id:
        raise ArtifactPackageError("manifest package ID mismatch")

    artifacts: list[GeneratedArtifact] = []
    for entry in manifest.entries:
        artifact_file = _safe_join(root, entry.relative_path)
        try:
            artifact = GeneratedArtifact.model_validate_json(
                artifact_file.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ArtifactPackageError(
                f"invalid artifact file {entry.relative_path}: {exc}"
            ) from exc
        if artifact.artifact_id != entry.artifact_id:
            raise ArtifactPackageError("artifact ID does not match manifest")
        if compute_artifact_digest(artifact) != entry.content_digest:
            raise ArtifactPackageError(
                f"artifact digest mismatch: {artifact.artifact_id}"
            )
        schema = validate_generated_artifact(artifact)
        if not schema.valid:
            raise ArtifactPackageError(
                f"invalid generated definition {artifact.artifact_id}"
            )
        artifacts.append(artifact)

    return GeneratedArtifactPackage(
        package_id=manifest.package_id,
        artifacts=artifacts,
        manifest=manifest,
        output_directory=str(root),
    )


def verify_saved_package(
    expected: GeneratedArtifactPackage, output_dir: Path
) -> GeneratedArtifactPackage:
    """Verify the approved package and reject missing/modified/extra files."""
    root = Path(output_dir).resolve()
    manifest_relative = f"manifests/{safe_filename(expected.package_id)}.json"
    restored = read_package(root, manifest_relative)
    if restored.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ArtifactPackageError("saved package content differs from plan package")

    expected_files = {
        entry.relative_path for entry in expected.manifest.entries
    }
    actual_files: set[str] = set()
    for directory in _PACKAGE_DIRECTORIES:
        if directory == "manifests":
            continue
        directory_path = _safe_join(root, directory)
        if not directory_path.exists():
            continue
        for path in directory_path.glob("*.json"):
            actual_files.add(path.relative_to(root).as_posix())

    # Multiple valid packages may share the configured root. Every artifact
    # file must be referenced by some manifest; unowned files are rejected.
    allowed_files: set[str] = set()
    manifests_dir = _safe_join(root, "manifests")
    for path in manifests_dir.glob("*.json"):
        try:
            manifest = ArtifactManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        allowed_files.update(entry.relative_path for entry in manifest.entries)
    unexpected = sorted(actual_files - allowed_files)
    missing = sorted(expected_files - actual_files)
    if missing:
        raise ArtifactPackageError(f"missing package files: {missing}")
    if unexpected:
        raise ArtifactPackageError(f"unexpected package files: {unexpected}")
    return restored

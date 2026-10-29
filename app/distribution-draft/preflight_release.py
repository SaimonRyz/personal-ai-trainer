"""Read-only inventory of a split release candidate; never installs or downloads."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP = Path(__file__).resolve().parents[1]
PACKAGE = APP.parent


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_install_state(relative: Path) -> bool:
    parts = relative.parts
    if parts and (parts[0] == ".git" or relative.name == ".DS_Store"
                  or any(part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
                         for part in parts)):
        return True
    if not parts or parts[0] != "app":
        return False
    tail = parts[1:]
    return (
        tail in {(".env",), ("RELEASE_BUILD.json",)}
        or len(tail) > 1 and tail[0] in {".venv", "wheelhouse"}
        or len(tail) > 2 and tail[:2] == ("tmp", "release-lifecycle")
    )


def package_integrity(expected_manifest_sha256: str | None) -> dict[str, Any]:
    manifest_path = PACKAGE / "PACKAGE_MANIFEST.json"
    if (
        PACKAGE.is_symlink() or APP.is_symlink()
        or manifest_path.is_symlink() or not manifest_path.is_file()
    ):
        return {"state": "NOT_VERIFIED", "reason": "package_manifest_missing"}
    actual_hash = _sha(manifest_path)
    if expected_manifest_sha256 is not None and actual_hash != expected_manifest_sha256:
        return {"state": "INVALID", "reason": "external_manifest_digest_mismatch"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        if manifest["schema_version"] != 1 or not isinstance(files, dict):
            raise ValueError("unsupported manifest")
        expected_paths = {"PACKAGE_MANIFEST.json", *files}
        actual_paths = set()
        for entry in PACKAGE.rglob("*"):
            relative = entry.relative_to(PACKAGE)
            if _is_install_state(relative):
                if relative.parts[:2] == ("app", "wheelhouse") and entry.is_symlink():
                    raise ValueError("linked wheelhouse entry")
                continue
            if entry.is_symlink():
                raise ValueError("linked package entry")
            if entry.is_file():
                actual_paths.add(relative.as_posix())
        if actual_paths != expected_paths:
            raise ValueError("package contains unlisted or missing files")
        for relative, metadata in files.items():
            relative_path = Path(relative)
            path = PACKAGE / relative_path
            current = PACKAGE
            linked_parent = False
            for part in relative_path.parts[:-1]:
                current /= part
                linked_parent = linked_parent or current.is_symlink()
            if (
                not isinstance(relative, str)
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or linked_parent
                or path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != metadata["size"]
                or _sha(path) != metadata["sha256"]
            ):
                raise ValueError("package file mismatch")
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "INVALID", "reason": "package_files_differ"}
    return {
        "state": "VERIFIED" if expected_manifest_sha256 else "SELF_CONSISTENT_ONLY",
        "file_count": len(files),
        "manifest_sha256": actual_hash,
    }


def _command(argv: list[str], *, limit: int = 300) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()[:limit]


def _port_state(lsof: str | None, port: int) -> str:
    if lsof is None:
        return "NOT_CHECKED"
    try:
        result = subprocess.run(
            [lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "NOT_CHECKED"
    if result.returncode == 0 and result.stdout.strip():
        return "OCCUPIED_REQUIRES_OWNERSHIP_REVIEW"
    if result.returncode == 1 and not result.stderr.strip():
        return "FREE"
    return "NOT_CHECKED"


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def venv_package_inventory(virtualenv: Path) -> dict[str, Any]:
    """Compare installed distribution metadata without importing project modules."""
    site = virtualenv / "lib/python3.12/site-packages"
    if any(path.is_symlink() for path in (virtualenv, virtualenv / "lib",
                                          virtualenv / "lib/python3.12", site)):
        return {"state": "UNSAFE"}
    if not site.is_dir():
        return {"state": "NOT_VERIFIED", "reason": "site_packages_missing"}
    try:
        plan = json.loads(
            (APP / "distribution-draft/RELEASE_WHEEL_PLAN.json").read_text(encoding="utf-8")
        )
        project = (APP / "pyproject.toml").read_text(encoding="utf-8")
        project_section = project.split("[project]", 1)[1].split("\n[", 1)[0]
        name_match = re.search(r'^name\s*=\s*"([^"\n]+)"', project_section, re.M)
        version_match = re.search(r'^version\s*=\s*"([^"\n]+)"', project_section, re.M)
        if plan["schema_version"] != 1 or not name_match or not version_match:
            raise ValueError("invalid release metadata")
        expected = {
            _normalized_name(item["name"]): item["version"]
            for item in plan["runtime"]["macos/arm64"] + plan["build"]
        }
        expected[_normalized_name(name_match.group(1))] = version_match.group(1)
        installed: dict[str, str] = {}
        for entry in site.iterdir():
            if entry.name.endswith(".dist-info") and entry.is_symlink():
                return {"state": "UNSAFE"}
        for distribution in importlib.metadata.distributions(path=[str(site)]):
            name = distribution.metadata.get("Name")
            if not name:
                raise ValueError("distribution without name")
            normalized = _normalized_name(name)
            if normalized in installed:
                raise ValueError("duplicate installed distribution")
            installed[normalized] = distribution.version
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {"state": "NOT_VERIFIED", "reason": "installed_metadata_unreadable"}
    missing = sorted(name for name in expected if name not in installed)
    different = sorted(name for name in expected if name in installed
                       and installed[name] != expected[name])
    extra = sorted(name for name in installed if name not in expected)
    return {"state": "VERIFIED" if not (missing or different or extra) else "MISMATCH",
            "missing": missing, "different": different, "extra": extra}


def _venv_prefix_matches(interpreter: Path, virtualenv: Path) -> bool:
    prefix = _command([str(interpreter), "-I", "-c", "import sys; print(sys.prefix)"])
    return bool(prefix and Path(prefix).is_absolute()
                and Path(prefix).resolve() == virtualenv.resolve())


def environment_inventory() -> dict[str, Any]:
    system = platform.system()
    machine = platform.machine()
    uv = shutil.which("uv")
    if uv is None:
        user_uv = Path.home() / ".local/bin/uv"
        if user_uv.is_file() and not user_uv.is_symlink() and os.access(user_uv, os.X_OK):
            uv = str(user_uv)
    docker = shutil.which("docker")
    if docker is None:
        desktop_cli = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
        if desktop_cli.is_file() and not desktop_cli.is_symlink():
            docker = str(desktop_cli)
    python = shutil.which("python3.12")
    if python is None and uv:
        candidate = _command([uv, "python", "find", "--no-python-downloads", "3.12.14"])
        if candidate and Path(candidate).is_file():
            python = candidate
    python_version = _command([python, "--version"]) if python else None
    uv_version_raw = _command([uv, "-V"]) if uv else None
    uv_match = re.fullmatch(r"uv (\d+\.\d+\.\d+)(?: \([^\n]*\))?", uv_version_raw or "")
    uv_version = f"uv {uv_match.group(1)}" if uv_match else None
    docker_client_raw = _command([docker, "--version"]) if docker else None
    docker_client_match = re.fullmatch(
        r"Docker version (\d+\.\d+\.\d+), build [^\n]+",
        docker_client_raw or "",
    )
    docker_client = docker_client_match.group(1) if docker_client_match else None
    docker_server = (
        _command([docker, "version", "--format", "{{.Server.Version}}"])
        if docker else None
    )
    compose = _command([docker, "compose", "version", "--short"]) if docker else None
    context = _command([docker, "context", "show"]) if docker else None
    endpoint_text = (
        _command([docker, "context", "inspect", "desktop-linux", "--format",
                  "{{json .Endpoints.docker.Host}}"])
        if docker and context == "desktop-linux" else None
    )
    try:
        endpoint = json.loads(endpoint_text) if endpoint_text else None
    except ValueError:
        endpoint = None
    local_endpoint = endpoint == f"unix://{Path.home()}/.docker/run/docker.sock"
    overrides_set = any(
        os.environ.get(name)
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY")
    )
    local_docker = bool(docker_server and local_endpoint and not overrides_set)
    env_path = APP / ".env"
    env_metadata = "ABSENT"
    if env_path.exists() or env_path.is_symlink():
        if env_path.is_symlink() or not env_path.is_file():
            env_metadata = "UNSAFE"
        else:
            info = env_path.stat()
            env_metadata = (
                "PRESENT_0600" if info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 else "UNSAFE"
            )
    virtualenv = APP / ".venv"
    if virtualenv.is_symlink() or (virtualenv.exists() and not virtualenv.is_dir()):
        venv_state = "UNSAFE"
    elif not virtualenv.exists():
        venv_state = "ABSENT"
    else:
        interpreter = virtualenv / "bin/python"
        venv_state = (
            "PRESENT_3_12_14"
            if interpreter.is_file() and _command([str(interpreter), "--version"])
            == "Python 3.12.14" and _venv_prefix_matches(interpreter, virtualenv)
            else "PRESENT_UNVERIFIED"
        )
    venv_packages = (
        venv_package_inventory(virtualenv) if venv_state == "PRESENT_3_12_14"
        else {"state": "NOT_CHECKED"}
    )
    codex_app = Path("/Applications/Codex.app")
    codex_present = bool(shutil.which("codex")) or (
        codex_app.is_dir() and not codex_app.is_symlink()
    )
    return {
        "platform": {"system": system, "machine": machine,
                     "supported": system == "Darwin" and machine == "arm64"},
        "python_3_12": python_version,
        "uv": uv_version,
        "uv_detail": uv_version_raw,
        "docker_client": docker_client,
        "docker_engine": docker_server,
        "docker_compose": compose,
        "docker_context": context,
        "docker_endpoint_local": local_endpoint,
        "docker_overrides_set": overrides_set,
        "local_docker": local_docker,
        "env_file": env_metadata,
        "venv": venv_state,
        "venv_packages": venv_packages,
        "codex_present": codex_present,
    }


def local_state_inventory(local_docker: bool) -> dict[str, Any]:
    """Report possible conflicts without opening ports or altering Docker state."""
    lsof = shutil.which("lsof")
    ports = {str(port): _port_state(lsof, port) for port in (8767, 8768)}
    volume_name = "personal-ai-trainer-alpha_trainer_postgres_data"
    volume_state = "NOT_CHECKED"
    if local_docker:
        docker = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
        listed = _command(
            [docker, "volume", "ls", "--filter", f"name=^{volume_name}$",
             "--format", "{{.Name}}"],
        )
        inspected = (
            _command([docker, "volume", "inspect", "--format", "{{json .}}", volume_name],
                     limit=100_000)
            if listed == volume_name else None
        )
        try:
            volume = json.loads(inspected) if inspected else None
        except ValueError:
            volume = None
        if listed == "":
            volume_state = "ABSENT"
        elif volume is None:
            volume_state = "NOT_VERIFIED"
        elif not isinstance(volume, dict):
            volume_state = "UNVERIFIED"
        else:
            labels = volume.get("Labels") or {}
            volume_state = (
                "PRESENT_EXPECTED_LABELS_REQUIRES_OWNERSHIP_REVIEW"
                if volume.get("Name") == volume_name
                and labels.get("com.docker.compose.project") == "personal-ai-trainer-alpha"
                and labels.get("com.docker.compose.volume") == "trainer_postgres_data"
                else "OWNERSHIP_MISMATCH"
            )
    return {"loopback_ports": ports, "expected_volume": volume_name,
            "volume_state": volume_state}


def artifact_inventory(local_docker: bool) -> dict[str, Any]:
    if (APP / "wheelhouse").is_symlink():
        raise ValueError("linked wheelhouse is forbidden")
    plan_path = APP / "distribution-draft/RELEASE_WHEEL_PLAN.json"
    pins_path = APP / "distribution-draft/RELEASE_INPUTS.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    pins = json.loads(pins_path.read_text(encoding="utf-8"))
    if plan["schema_version"] != 1 or pins["schema_version"] != 1:
        raise ValueError("release artifact schema is unsupported")
    if plan["uv_lock_sha256"] != pins["uv_lock_sha256"]:
        raise ValueError("release wheel plan and lock pin differ")
    missing_wheels: list[dict[str, Any]] = []
    unexpected_wheelhouse_entries: list[str] = []
    wheelhouse = APP / "wheelhouse"
    if wheelhouse.exists():
        if not wheelhouse.is_dir():
            raise ValueError("wheelhouse is not a directory")
        for entry in wheelhouse.iterdir():
            if entry.is_symlink():
                raise ValueError("linked wheelhouse entry is forbidden")
            if entry.name not in {"macos-arm64", "linux-arm64"}:
                unexpected_wheelhouse_entries.append(entry.name)
    for target, folder_name in (("macos/arm64", "macos-arm64"), ("linux/arm64", "linux-arm64")):
        folder = APP / "wheelhouse" / folder_name
        if folder.is_symlink():
            raise ValueError("linked wheelhouse is forbidden")
        expected_names = {"runtime-requirements.txt"}
        for artifact in plan["runtime"][target] + plan["build"]:
            parsed = urlparse(artifact["url"])
            if (
                parsed.scheme != "https"
                or parsed.hostname != "files.pythonhosted.org"
                or parsed.query or parsed.fragment
                or parsed.path.rsplit("/", 1)[-1] != artifact["filename"]
                or not artifact["filename"].endswith(".whl")
                or not isinstance(artifact["size"], int) or artifact["size"] <= 0
                or not isinstance(artifact["hash"], str)
                or len(artifact["hash"]) != 71
                or not artifact["hash"].startswith("sha256:")
            ):
                raise ValueError("release wheel metadata is invalid")
            expected_names.add(artifact["filename"])
            path = folder / artifact["filename"]
            if path.is_symlink():
                raise ValueError("linked wheel is forbidden")
            if path.exists():
                if not path.is_file() or path.stat().st_size != artifact["size"] or (
                    "sha256:" + _sha(path) != artifact["hash"]
                ):
                    raise ValueError("existing wheel differs from pinned artifact")
            else:
                missing_wheels.append({"target": target, **artifact})
        if folder.exists():
            if not folder.is_dir():
                raise ValueError("wheelhouse target is not a directory")
            for entry in folder.iterdir():
                if entry.is_symlink():
                    raise ValueError("linked wheelhouse entry is forbidden")
                if entry.name not in expected_names:
                    unexpected_wheelhouse_entries.append(f"{folder_name}/{entry.name}")
    missing_images = []
    verified_images: list[dict[str, Any]] = []
    for key in ("python_image", "uv_image", "postgres_image"):
        reference = pins[key]["reference"]
        expected_image_id = pins[key]["expected_image_id"]
        platform_digest = pins[key]["platform_manifest_digest"]
        compressed_bytes = pins[key]["compressed_layer_bytes"]
        if (
            not isinstance(reference, str)
            or not isinstance(expected_image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id)
            or not isinstance(platform_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", platform_digest)
            or type(compressed_bytes) is not int
            or compressed_bytes <= 0
        ):
            raise ValueError("release image metadata is invalid")
        image = {"name": key, "reference": reference,
                 "platform_manifest_digest": platform_digest,
                 "expected_image_id": expected_image_id,
                 "compressed_layer_bytes": compressed_bytes}
        if not local_docker:
            image["state"] = "NOT_CHECKED"
            missing_images.append(image)
            continue
        docker = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
        inspected = _command([docker, "image", "inspect", "--format",
                              "{{json .}}", reference], limit=1_000_000)
        try:
            details = json.loads(inspected) if inspected else None
        except ValueError:
            details = None
        if not isinstance(details, dict):
            image["state"] = "MISSING_OR_UNVERIFIED"
            missing_images.append(image)
            continue
        digests = details.get("RepoDigests") or []
        pinned_reference_present = (
            details.get("Os") == "linux" and details.get("Architecture") == "arm64"
            and reference in digests
        )
        if pinned_reference_present and details.get("Id") == expected_image_id:
            image["state"] = "VERIFIED"
            verified_images.append(image)
        else:
            image["state"] = (
                "PRESENT_NEEDS_CONFIG_VERIFICATION"
                if pinned_reference_present else "MISSING_OR_UNVERIFIED"
            )
            missing_images.append(image)
    return {
        "missing_wheels": missing_wheels,
        "missing_wheel_count": len(missing_wheels),
        "missing_wheel_bytes": sum(item["size"] for item in missing_wheels),
        "unexpected_wheelhouse_entries": sorted(unexpected_wheelhouse_entries),
        "missing_or_unverified_images": missing_images,
        "verified_images": verified_images,
        "docker_image_download_bytes": sum(
            item["compressed_layer_bytes"] for item in missing_images
        ),
        "docker_image_download_bytes_basis": (
            "sum_of_declared_compressed_layers_before_cache_deduplication"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args()
    expected = args.expected_manifest_sha256
    if expected is not None and (
        len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected)
    ):
        raise ValueError("expected manifest SHA-256 must be 64 lowercase hex characters")
    package = package_integrity(expected)
    environment = environment_inventory()
    local_state = local_state_inventory(environment["local_docker"])
    artifacts = artifact_inventory(environment["local_docker"]) if package["state"] in {
        "VERIFIED", "SELF_CONSISTENT_ONLY"
    } else None
    blockers = []
    if package["state"] not in {"VERIFIED", "SELF_CONSISTENT_ONLY"}:
        blockers.append("package_integrity_invalid_or_missing")
    if not environment["platform"]["supported"]:
        blockers.append("unsupported_platform")
    if environment["python_3_12"] != "Python 3.12.14":
        blockers.append("python_3_12_14_unavailable")
    if environment["uv"] != "uv 0.12.24":
        blockers.append("uv_0_12_24_unavailable")
    if not environment["docker_engine"] or not environment["docker_compose"]:
        blockers.append("docker_engine_or_compose_unavailable")
    if not environment["local_docker"]:
        blockers.append("local_docker_context_unverified")
    if not environment["codex_present"]:
        blockers.append("codex_not_detected")
    if environment["venv"] != "PRESENT_3_12_14":
        blockers.append("local_venv_missing_or_unverified")
    elif environment["venv_packages"]["state"] != "VERIFIED":
        blockers.append("local_venv_packages_mismatch_or_unverified")
    if environment["env_file"] == "ABSENT":
        blockers.append("local_env_file_absent")
    if environment["venv"] == "UNSAFE" or environment["env_file"] == "UNSAFE":
        blockers.append("local_installation_file_unsafe")
    if local_state["volume_state"] not in {"NOT_CHECKED", "ABSENT", "NOT_VERIFIED"}:
        blockers.append("existing_volume_requires_ownership_review")
    if local_state["volume_state"] == "NOT_VERIFIED":
        blockers.append("volume_state_not_verified")
    if any(value == "NOT_CHECKED" for value in local_state["loopback_ports"].values()):
        blockers.append("loopback_port_state_not_verified")
    if any(value == "OCCUPIED_REQUIRES_OWNERSHIP_REVIEW"
           for value in local_state["loopback_ports"].values()):
        blockers.append("occupied_port_requires_ownership_review")
    if artifacts and (artifacts["missing_wheels"] or artifacts["missing_or_unverified_images"]):
        blockers.append("release_artifacts_missing_or_unverified")
    if artifacts and artifacts["unexpected_wheelhouse_entries"]:
        blockers.append("unexpected_wheelhouse_entries_require_review")
    print(json.dumps({"status": "NOT_READY", "read_only": True,
                      "package": package, "environment": environment,
                      "local_state": local_state, "artifacts": artifacts,
                      "blockers": blockers}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"RELEASE_PREFLIGHT_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error

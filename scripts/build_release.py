#!/usr/bin/python3
# Builds zip release files for moontest

import os
import sys
import argparse
import shutil
import tempfile
import json
import pathlib
import time
import traceback
import subprocess
import re
import hashlib
from typing import Dict, Any, List, Set, Optional

# Path to the application root
APP_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))

# Dirs and exts to ignore when calculating the repo hash.  These
# items will be recursively ignored
IGNORE_DIRS = ["__pycache__"]
IGNORE_EXTS = [".o", ".so", ".pyc", ".pyo", ".pyd", ".yml", ".yaml"]

# Files not to include in the source package
SKIP_FILES = [".gitignore", ".gitattributes", ".readthedocs.yaml",
              "mkdocs.yml", "__pycache__"]

# The GitHub API url for current releases in this repo
RELEASE_URL = "https://api.github.com/repos/Arksine/moontest/releases"

# GitHub owner/repo.
OWNER_REPOS = {
    'moontest': "arksine/moontest"
}

# Install scripts for each app hosted by this repo.  This module will
# attempt to parse dependencies from the script.  The script must follow
# the same pattern as Klipper's install script.  It is acceptable
# for an application to skip this step if it has no dependencies, or
# manually populate the "dependencies" file.
INSTALL_SCRIPTS = {}
#INSTALL_SCRIPTS = {
#    'klippy': {
#        'debian': "install-octopi.sh",
#        'arch': "install-arch.sh",
#        'centos': "install-centos.sh"
#    },
#    'moonraker': {
#        'debian': "install-moonraker.sh"
#    }
#}

# Helpers for generating the COMMIT_LOG
GIT_MAX_LOG_CNT = 100
GIT_LOG_FMT = \
    "sha:%H%x1Dauthor:%an%x1Ddate:%ct%x1Dsubject:%s%x1Dmessage:%b%x1E"

# Parse the git version from the command line.  This code
# is borrowed from Klipper.
def retreive_git_version(source_path: str) -> str:
    # Obtain version info from "git" program
    prog = ('git', '-C', source_path, 'describe', '--always',
            '--tags', '--long', '--dirty')
    process = subprocess.Popen(prog, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    ver, err = process.communicate()
    retcode = process.wait()
    if retcode == 0:
        return ver.strip().decode()
    raise Exception(f"Failed to retreive git version: {err.decode()}")

# Method to recursively calculate the hash of a directory
def hash_directory(dir_path: str,
                   ignore_exts: List[str],
                   ignore_dirs: List[str]
                   ) -> str:
    checksum = hashlib.blake2s()
    if not os.path.exists(dir_path):
        return ""
    for dpath, dnames, fnames in os.walk(dir_path):
        valid_dirs: List[str] = []
        for dname in sorted(dnames):
            if dname[0] == '.' or dname in ignore_dirs:
                continue
            valid_dirs.append(dname)
        dnames[:] = valid_dirs
        for fname in sorted(fnames):
            ext = os.path.splitext(fname)[-1].lower()
            if fname[0] == '.' or ext in ignore_exts:
                continue
            fpath = pathlib.Path(os.path.join(dpath, fname))
            try:
                checksum.update(fpath.read_bytes())
            except Exception:
                pass
    return checksum.hexdigest()

class CopyIgnore:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = root_dir

    def __call__(self, dir_path: str, dir_items: List[str]) -> List[str]:
        ignored: List[str] = []
        for item in dir_items:
            if item in SKIP_FILES:
                ignored.append(item)
            elif dir_path == self.root_dir:
                full_path = os.path.join(dir_path, item)
                # Ignore all hidden directories in the root
                if os.path.isdir(full_path) and item[0] == ".":
                    ignored.append(item)
        return ignored

def search_install_script(data: str,
                          regex: str,
                          exclude: str
                          ) -> List[str]:
    items: Set[str] = set()
    lines: List[str] = re.findall(regex, data)
    for line in lines:
        items.update(line.strip().split())
    try:
        items.remove(exclude)
    except KeyError:
        pass
    return list(items)

def generate_dependency_info(repo_path: str, app_name: str) -> None:
    if app_name not in INSTALL_SCRIPTS:
        # no dependencies for this application
        return
    inst_scripts = INSTALL_SCRIPTS[app_name]
    package_info: Dict[str, Any] = {}
    for distro, script_name in inst_scripts.items():
        script_path = os.path.join(repo_path, "scripts", script_name)
        script = pathlib.Path(script_path)
        if not script.exists():
            continue
        data = script.read_text()
        packages: List[str] = search_install_script(
            data, r'PKGLIST="(.*)"', "${PKGLIST}")
        package_info[distro] = {'packages': sorted(packages)}
        if distro == "arch":
            aur_packages: List[str] = search_install_script(
                data, r'AURLIST="(.*)"', "${AURLIST}")
            package_info[distro]['aur_packages'] = sorted(aur_packages)
    req_file_name = os.path.join(repo_path, "scripts",
                                 f"{app_name}-requirements.txt")
    req_file = pathlib.Path(req_file_name)
    if req_file.exists():
        python_reqs: List[str] = []
        if req_file.exists():
            req_data = req_file.read_text()
            lines = [line.strip() for line in req_data.split('\n')
                    if line.strip()]
            for line in lines:
                comment_idx = line.find('#')
                if comment_idx == 0:
                    continue
                if comment_idx > 0:
                    line = line[:comment_idx].strip()
                python_reqs.append(line)
        package_info['python'] = sorted(python_reqs)
    dep_file = pathlib.Path(os.path.join(repo_path, ".dependencies"))
    dep_file.write_text(json.dumps(package_info))

def clean_repo(path: str) -> None:
    # Obtain version info from "git" program
    prog = ('git', '-C', path, 'clean', '-x', '-f', '-d')
    process = subprocess.Popen(prog, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=path)
    retcode = process.wait()
    if retcode != 0:
        print(f"Error running git clean: {path}")

def get_releases() -> List[Dict[str, Any]]:
    print("Fetching Release List...")
    prog = ('curl', '-H', "Accept: application/vnd.github.v3+json",
            RELEASE_URL)
    process = subprocess.Popen(prog, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    response, err = process.communicate()
    retcode = process.wait()
    if retcode != 0:
        print(f"Release list request returned with code {retcode},"
              f" response:\n{err.decode()}")
        return []
    releases = json.loads(response.decode().strip())
    print(f"Found {len(releases)} releases")
    return releases

def get_last_release_info(repo_version: str,
                          is_beta: bool,
                          releases: List[Dict[str, Any]]
                          ) -> Dict[str, Any]:
    print("Searching for previous release assets...")
    cur_tag, commit_count = repo_version.split('-', 2)[:2]
    release_assets = []
    matched_tag: Optional[str] = None
    for release in releases:
        if int(commit_count) != 0:
            # This is build is not being done against a fresh release,
            # return release info from a matching tag
            if release['tag_name'] == cur_tag:
                release_assets = release['assets']
                matched_tag = cur_tag
                break
        else:
            # Get the most recent non-matching tag
            if release['tag_name'] == cur_tag:
                continue
            if is_beta or not release['prerelease']:
                # Get the last tagged release.  If we are building a beta,
                # that is the most recent release.  Otherwise we should
                # omit pre-releases
                release_assets = release['assets']
                matched_tag = release['tag_name']
                break
    if matched_tag is None:
        print("No matching release found")
        matched_tag = "No Tag"
    else:
        print(f"Found release: {matched_tag}")

    asset_url: Optional[str] = None
    content_type: str = ""
    for asset in release_assets:
        if asset['name'] == "RELEASE_INFO":
            asset_url = asset['browser_download_url']
            content_type = asset['content_type']
            break
    if asset_url is None:
        print(f"RELEASE_INFO asset not found in release: {matched_tag}")
        return {}
        # This build is prior to a tagged release, so fetch the current tag
    print(f"Release Info Download URL: {asset_url}")
    prog = ('curl', '-L', '-H', f"Accept: {content_type}", asset_url)
    process = subprocess.Popen(prog, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    response, err = process.communicate()
    retcode = process.wait()
    if retcode != 0:
        print("Request for release info failed")
        return {}
    resp = response.decode().strip()
    print(f"Found Info for release {matched_tag}")
    return json.loads(resp)

def get_commit_log(path: str,
                   release_info: Dict[str, Any]
                   ) -> List[Dict[str, Any]]:
    print(f"Preparing commit log for {path.split('/')[-1]}")
    start_sha = release_info.get('commit_hash', None)
    prog = ['git', '-C', path, 'log', f'--format={GIT_LOG_FMT}',
            f'--max-count={GIT_MAX_LOG_CNT}']
    if start_sha is not None:
        prog = ['git', '-C', path, 'log', f'{start_sha}..HEAD',
                f'--format={GIT_LOG_FMT}', f'--max-count={GIT_MAX_LOG_CNT}']
    process = subprocess.Popen(prog, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=path)
    response, err = process.communicate()
    retcode = process.wait()
    if retcode != 0:
        return []
    resp = response.decode().strip()
    commit_log: List[Dict[str, Any]] = []
    for log_entry in resp.split('\x1E'):
        log_entry = log_entry.strip()
        if not log_entry:
            continue
        log_items = [li.strip() for li in log_entry.split('\x1D')
                     if li.strip()]
        cbh = [li.split(':', 1) for li in log_items]
        commit_log.append(dict(cbh))  # type: ignore
    print(f"Found {len(commit_log)} commits")
    return commit_log

def get_commit_hash(path: str) -> str:
    prog = ('git', '-C', path, 'rev-parse', 'HEAD')
    process = subprocess.Popen(prog, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd=path)
    commit_hash, err = process.communicate()
    retcode = process.wait()
    if retcode == 0:
        return commit_hash.strip().decode()
    raise Exception(f"Failed to get commit hash: {commit_hash.decode()}")

def generate_version_info(path: str,
                          app_name: str,
                          channel: str,
                          release_tag: Optional[str] = None
                          ) -> Dict[str, Any]:
    print(f"Generating version info: {app_name}")
    clean_repo(path)
    owner_repo = OWNER_REPOS[app_name]
    curtime = int(time.time())
    date_str = time.strftime("%Y%m%d", time.gmtime(curtime))
    version = retreive_git_version(path)
    if release_tag is None:
        release_tag = version.split('-')[0]
    source_hash = hash_directory(path, IGNORE_EXTS, IGNORE_DIRS)
    long_version = f"{version}-moonraker-{date_str}"
    release_info = {
        'git_version': version,
        'long_version': long_version,
        'commit_hash': get_commit_hash(path),
        'source_checksum': source_hash,
        'ignored_exts': IGNORE_EXTS,
        'ignored_dirs': IGNORE_DIRS,
        'build_date': curtime,
        'channel': channel,
        'owner_repo': owner_repo,
        'host_repo': OWNER_REPOS['moontest'],
        'release_tag': release_tag
    }
    rfile = pathlib.Path(os.path.join(path, ".release_info"))
    rfile.write_text(json.dumps(release_info))
    generate_dependency_info(path, app_name)
    return release_info

def create_zip(repo_path: str,
               repo_name: str,
               output_path: str
               ) -> None:
    print(f"Creating Zip Release: {repo_name}")
    zip_path = os.path.join(output_path, repo_name)
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_path = os.path.join(tmp_dir, repo_name)
        ingore_cb = CopyIgnore(repo_path)
        shutil.copytree(repo_path, dest_path, ignore=ingore_cb)
        shutil.make_archive(zip_path, "zip", root_dir=dest_path)

def main() -> None:
    # Parse start arguments
    parser = argparse.ArgumentParser(
        description="Generates zip release for moontest")
    parser.add_argument(
        "-o", "--output", default=os.path.join(APP_PATH, ".dist"),
        metavar='<output_path>', help="Path to output directory")
    parser.add_argument(
        "-b", "--beta", action='store_true',
        help="Tag release as beta")
    args = parser.parse_args()
    opath: str = os.path.abspath(os.path.expanduser(args.output))
    is_beta: bool = args.beta
    channel = "beta" if is_beta else "stable"
    if not os.path.exists(opath):
        print(f"Invalid output path: {opath}")
        sys.exit(-1)
    releases = get_releases()
    all_info: Dict[str, Dict[str, Any]] = {}
    try:
        print("Generating moontest Zip Distribution...")
        all_info['moontest'] = generate_version_info(
            APP_PATH, "moontest", channel)
        create_zip(APP_PATH, 'moontest', opath)
        info_file = pathlib.Path(os.path.join(opath, "RELEASE_INFO"))
        info_file.write_text(json.dumps(all_info))
        last_rinfo = get_last_release_info(
            all_info['moontest']['git_version'], is_beta, releases)
        commit_log = {}
        commit_log['moontest'] = get_commit_log(
            APP_PATH, last_rinfo.get('moontest', {}))
        clog_file = pathlib.Path(os.path.join(opath, "COMMIT_LOG"))
        clog_file.write_text(json.dumps(commit_log))
    except Exception:
        print("Error Creating Zip Distribution")
        traceback.print_exc(file=sys.stdout)
        sys.exit(-1)
    print(f"Build Complete.  Files are located at '{opath}'")


if __name__ == "__main__":
    main()

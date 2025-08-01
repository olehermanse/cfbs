"""
Functions ending in "_command" are dynamically included in the list of commands
in main.py for -h/--help/help.
"""

import os
import re
import copy
import logging as log
import json
import functools
from typing import List, Union
from collections import OrderedDict
from cfbs.analyze import analyze_policyset
from cfbs.args import get_args

from cfbs.cfbs_json import CFBSJson
from cfbs.updates import ModuleUpdates, update_module
from cfbs.utils import (
    CFBSNetworkError,
    CFBSUserError,
    cfbs_filename,
    is_cfbs_repo,
    read_json,
    CFBSExitError,
    strip_right,
    pad_right,
    CFBSProgrammerError,
    get_json,
    write_json,
    rm,
    cp,
    sh,
    is_a_commit_hash,
)

from cfbs.pretty import (
    pretty,
    pretty_check_file,
    pretty_file,
    CFBS_DEFAULT_SORTING_RULES,
)
from cfbs.build import (
    init_out_folder,
    perform_build,
)
from cfbs.cfbs_config import CFBSConfig, CFBSReturnWithoutCommit
from cfbs.validate import validate_config
from cfbs.internal_file_management import (
    clone_url_repo,
    SUPPORTED_URI_SCHEMES,
    fetch_archive,
    get_download_path,
    local_module_copy,
    SUPPORTED_ARCHIVES,
)
from cfbs.index import _VERSION_INDEX, Index
from cfbs.git import (
    git_exists,
    is_git_repo,
    git_get_config,
    git_set_config,
    git_init,
    CFBSGitError,
    ls_remote,
)
from cfbs.git_magic import Result, commit_after_command, git_commit_maybe_prompt
from cfbs.prompts import YES_NO_CHOICES, prompt_user
from cfbs.module import Module, is_module_added_manually
from cfbs.masterfiles.generate_release_information import generate_release_information

_MODULES_URL = "https://archive.build.cfengine.com/modules"

PLURAL_S = lambda args, _: "s" if len(args[0]) > 1 else ""
FIRST_ARG = lambda args, _: "'%s'" % args[0]
FIRST_ARG_SLIST = lambda args, _: ", ".join("'%s'" % module for module in args[0])

_commands = OrderedDict()


# Decorator to specify that a function is a command (verb in the CLI)
# Adds the name + function pair to the global dict of commands
# Does not modify/wrap the function it decorates.
def cfbs_command(name):
    def inner(function):
        _commands[name] = function
        return function  # Unmodified, we've just added it to the dict

    return inner


def get_command_names():
    names = _commands.keys()
    return names


@cfbs_command("pretty")
def pretty_command(filenames: list, check: bool, keep_order: bool) -> int:
    if not filenames:
        raise CFBSExitError("Filenames missing for cfbs pretty command")

    sorting_rules = CFBS_DEFAULT_SORTING_RULES if keep_order else None
    num_files = 0
    for f in filenames:
        if not f or not f.endswith(".json"):
            raise CFBSExitError(
                "cfbs pretty command can only be used with .json files, not '%s'"
                % os.path.basename(f)
            )
        try:
            if check:
                if not pretty_check_file(f, sorting_rules):
                    num_files += 1
                    print("Would reformat %s" % f)
            else:
                pretty_file(f, sorting_rules)
        except FileNotFoundError:
            raise CFBSExitError("File '%s' not found" % f)
        except json.decoder.JSONDecodeError as ex:
            raise CFBSExitError("Error reading json file '{}': {}".format(f, ex))
    if check:
        print("Would reformat %d file(s)" % num_files)
        return 1 if num_files > 0 else 0
    return 0


@cfbs_command("init")
def init_command(index=None, masterfiles=None, non_interactive=False) -> int:
    if is_cfbs_repo():
        raise CFBSUserError("Already initialized - look at %s" % cfbs_filename())

    name = prompt_user(
        non_interactive,
        "Please enter the name of this CFEngine Build project",
        default="Example project",
    )
    description = prompt_user(
        non_interactive,
        "Please enter the description of this CFEngine Build project",
        default="Example description",
    )

    config = OrderedDict(
        {
            "name": name,
            "type": "policy-set",  # TODO: Prompt whether user wants to make a module
            "description": description,
            "build": [],
        }
    )
    if index:
        config["index"] = index

    do_git = get_args().git
    is_git = is_git_repo()
    if do_git is None:
        if is_git:
            git_ans = prompt_user(
                non_interactive,
                "This is a git repository. Do you want cfbs to make commits to it?",
                choices=YES_NO_CHOICES,
                default="yes",
            )
        else:
            git_ans = prompt_user(
                non_interactive,
                "Do you want cfbs to initialize a git repository and make commits to it?",
                choices=YES_NO_CHOICES,
                default="yes",
            )
        do_git = git_ans.lower() in ("yes", "y")
    else:
        assert do_git in ("yes", "no")
        do_git = True if do_git == "yes" else False

    if do_git is True:
        if not git_exists():
            print("Command 'git' was not found")
            return 1

        user_name = get_args().git_user_name
        if not user_name:
            user_name = git_get_config("user.name")
            user_name = prompt_user(
                non_interactive,
                "Please enter user name to use for git commits",
                default=user_name or "cfbs",
            )

        user_email = get_args().git_user_email
        if not user_email:
            user_email = git_get_config("user.email")
            node_name = os.uname().nodename
            user_email = prompt_user(
                non_interactive,
                "Please enter user email to use for git commits",
                default=user_email or ("cfbs@%s" % node_name),
            )

        if not is_git:
            try:
                git_init(user_name, user_email, description)
            except CFBSGitError as e:
                print(str(e))
                return 1
        else:
            if not git_set_config("user.name", user_name) or not git_set_config(
                "user.email", user_email
            ):
                print("Failed to set Git user name and email")
                return 1

    config["git"] = do_git

    data = pretty(config, CFBS_DEFAULT_SORTING_RULES) + "\n"
    with open(cfbs_filename(), "w") as f:
        f.write(data)
    assert is_cfbs_repo()

    if do_git:
        try:
            git_commit_maybe_prompt(
                "Initialized a new CFEngine Build project",
                non_interactive,
                [cfbs_filename()],
            )
        except CFBSGitError as e:
            print(str(e))
            os.unlink(cfbs_filename())
            return 1

    print(
        "Initialized an empty project called '{}' in '{}'".format(name, cfbs_filename())
    )

    """
    The CFBSConfig instance was initally created in main(). Back then
    cfbs.json did not exist, thus the instance is empty. Ensure it is reloaded
    now that the JSON exists.
    """
    CFBSConfig.reload()

    branch = None
    to_add = []
    if masterfiles is None:
        if prompt_user(
            non_interactive,
            "Do you wish to build on top of the default policy set, masterfiles? (Recommended)",
            choices=YES_NO_CHOICES,
            default="yes",
        ) in ("yes", "y"):
            to_add = ["masterfiles"]
        else:
            answer = prompt_user(
                non_interactive,
                "Specify policy set to use instead (empty to skip)",
                default="",
            )
            if answer:
                to_add = [answer]
    elif re.match(r"[0-9]+(\.[0-9]+){2}(\-[0-9]+)?", masterfiles):
        log.debug("--masterfiles=%s appears to be a version number" % masterfiles)
        to_add = ["masterfiles@%s" % masterfiles]
    elif masterfiles != "no":
        """This appears to be a branch. Thus we'll add masterfiles normally
        and try to do the necessary modifications needed afterwards. I.e.
        changing the 'repo' attribute to be 'url' and changing the commit to
        be the current HEAD of the upstream branch."""

        log.debug("--masterfiles=%s appears to be a branch" % masterfiles)
        branch = masterfiles
        to_add = ["masterfiles"]

    if branch is not None:
        remote = "https://github.com/cfengine/masterfiles"
        commit = ls_remote(remote, branch)
        if commit is None:
            raise CFBSExitError(
                "Failed to find branch or tag %s at remote %s" % (branch, remote)
            )
        log.debug("Current commit for masterfiles branch %s is %s" % (branch, commit))
        to_add = ["%s@%s" % (remote, commit), "masterfiles"]
    if to_add:
        result = add_command(to_add, added_by="cfbs init")
        assert not isinstance(
            result, Result
        ), "Our git decorators are confusing the type checkers"
        if result != 0:
            return result
        # TODO: Do we need to make commits here?

    return 0


@cfbs_command("status")
def status_command() -> int:
    config = CFBSConfig.get_instance()
    if validate_config(config, empty_build_list_ok=True) != 0:
        return 1
    config.warn_about_unknown_keys()
    print("Name:        %s" % config["name"])
    print("Description: %s" % config["description"])
    print("File:        %s" % cfbs_filename())
    if "index" in config:
        assert config.raw_data is not None
        index = config.raw_data["index"]

        if type(index) is str:
            print("Index:       %s" % index)
        else:
            print("Index:       %s" % "inline index in cfbs.json")

    modules = config.get("build")
    if not modules:
        return 0
    print("\nModules:")
    max_name_length = config.longest_module_key_length("name")
    max_version_length = config.longest_module_key_length("version")
    counter = 1
    for m in modules:
        if m["name"].startswith("./"):
            status = "Copied"
            version = "local"
            commit = pad_right("", 40)
        else:
            path = get_download_path(m)
            status = "Downloaded" if os.path.exists(path) else "Not downloaded"
            version = m.get("version", "")
            commit = m["commit"]
        name = pad_right(m["name"], max_name_length)
        version = pad_right(version, max_version_length)
        version_with_commit = version + " "
        if m["name"].startswith("./"):
            version_with_commit += " "
        else:
            version_with_commit += "/"
        version_with_commit += " " + commit
        print("%03d %s @ %s (%s)" % (counter, name, version_with_commit, status))
        counter += 1

    return 0


@cfbs_command("search")
def search_command(terms: list) -> int:
    index = CFBSConfig.get_instance().index
    results = {}

    # in order to gather all aliases, we must iterate over everything first
    for name, data in index.items():
        if "alias" in data:
            realname = data["alias"]
            if realname not in results:
                results[realname] = {}
            if "aliases" in results[realname]:
                results[realname]["aliases"].append(name)
            else:
                results[realname]["aliases"] = [name]
            continue
        if name in results:
            results[name]["description"] = data["description"]
        else:
            results[name] = {"description": data["description"], "aliases": []}

    filtered = {}
    if terms:
        for name in (
            name
            for name, data in results.items()
            if any((t for t in terms if t in name))
            or any((t for t in terms if any((s for s in data["aliases"] if t in s))))
        ):
            filtered[name] = results[name]
    else:
        filtered = results

    results = filtered
    for k, v in results.items():
        print("{}".format(k), end="")
        if any(v["aliases"]):
            print(" ({})".format(", ".join(v["aliases"])), end="")
        print(" - {}".format(v["description"]))

    return 0 if any(results) else 1


@cfbs_command("add")
@commit_after_command("Added module%s %s", [PLURAL_S, FIRST_ARG_SLIST])
def add_command(
    to_add: list,
    added_by="cfbs add",
    checksum=None,
) -> Union[Result, int]:
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    r = config.add_command(to_add, added_by, checksum)
    config.save()
    return r


@cfbs_command("remove")
@commit_after_command("Removed module%s %s", [PLURAL_S, FIRST_ARG_SLIST])
def remove_command(to_remove: List[str]):
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    if "build" not in config:
        raise CFBSExitError(
            'Cannot remove any modules because the "build" key is missing from cfbs.json'
        )
    modules = config["build"]

    def _get_dependents(dependency) -> list:
        if len(modules) < 2:
            return []

        def reduce_dependencies(a, b):
            result_b = [b["name"]] if dependency in b.get("dependencies", []) else []
            if type(a) is list:
                return a + result_b
            else:
                return (
                    [a["name"]] if dependency in a.get("dependencies", []) else []
                ) + result_b

        return functools.reduce(reduce_dependencies, modules)

    def _get_module_by_name(name) -> Union[dict, None]:
        if not name.startswith("./") and name.endswith(".cf") and os.path.exists(name):
            name = "./" + name

        for module in modules:
            if module["name"] == name:
                return module
        return None

    def _remove_module_user_prompt(module):
        dependents = _get_dependents(module["name"])
        return prompt_user(
            config.non_interactive,
            "Do you wish to remove '%s'?" % module["name"]
            + (
                " (The module is a dependency of the following module%s: %s)"
                % ("s" if len(dependents) > 1 else "", ", ".join(dependents))
                if dependents
                else ""
            ),
            choices=YES_NO_CHOICES,
            default="yes",
        )

    def _get_modules_by_url(name) -> list:
        r = []
        for module in modules:
            if "url" in module and module["url"] == name:
                r.append(module)
        return r

    num_removed = 0
    msg = ""
    files = []
    for name in to_remove:
        if name.startswith(SUPPORTED_URI_SCHEMES):
            matches = _get_modules_by_url(name)
            if not matches:
                raise CFBSExitError("Could not find module with URL '%s'" % name)
            for module in matches:
                answer = _remove_module_user_prompt(module)
                if answer.lower() in ("yes", "y"):
                    print("Removing module '%s'" % module["name"])
                    modules.remove(module)
                    msg += "\n - Removed module '%s'" % module["name"]
                    num_removed += 1
        else:
            module = _get_module_by_name(name)
            if module:
                answer = _remove_module_user_prompt(module)
                if answer.lower() in ("yes", "y"):
                    print("Removing module '%s'" % name)
                    modules.remove(module)
                    msg += "\n - Removed module '%s'" % module["name"]
                    num_removed += 1
            else:
                print("Module '%s' not found" % name)
        input_path = os.path.join(".", name, "input.json")
        if os.path.isfile(input_path) and prompt_user(
            config.non_interactive,
            "Module '%s' has input data '%s'. Do you want to remove it?"
            % (name, input_path),
            choices=YES_NO_CHOICES,
            default="no",
        ).lower() in ("yes", "y"):
            rm(input_path)
            files.append(input_path)
            msg += "\n - Removed input data for module '%s'" % name
            log.debug("Deleted module data '%s'" % input_path)

    num_lines = len(msg.strip().splitlines())
    changes_made = num_lines > 0
    if num_lines > 1:
        msg = "Removed %d modules\n" % num_removed + msg
    else:
        msg = msg[4:]  # Remove the '\n - ' part of the message

    config.save()
    if num_removed:
        try:
            _clean_unused_modules(config)
        except CFBSReturnWithoutCommit:
            pass
    return Result(0, changes_made, msg, files)


@cfbs_command("clean")
@commit_after_command("Cleaned unused modules")
def clean_command(config=None):
    return _clean_unused_modules(config)


def _clean_unused_modules(config=None):
    if not config:
        config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    if "build" not in config:
        log.warning('No "build" key with modules - nothing to clean')
        return 0
    modules = config["build"]
    if len(modules) == 0:
        return 0

    def _someone_needs_me(this) -> bool:
        if ("added_by" not in this) or is_module_added_manually(this["added_by"]):
            return True
        for other in modules:
            if "dependencies" not in other:
                continue
            if this["name"] in other["dependencies"]:
                return _someone_needs_me(other)
        return False

    to_remove = list()
    for module in modules:
        if not _someone_needs_me(module):
            to_remove.append(module)

    if not to_remove:
        raise CFBSReturnWithoutCommit(0)

    print("The following modules were added as dependencies but are no longer needed:")
    for module in to_remove:
        name = module["name"] if "name" in module else ""
        description = module["description"] if "description" in module else ""
        added_by = module["added_by"] if "added_by" in module else ""
        print("%s - %s - added by: %s" % (name, description, added_by))

    answer = prompt_user(
        config.non_interactive,
        "Do you wish to remove these modules?",
        choices=YES_NO_CHOICES,
        default="yes",
    )
    if answer.lower() in ("yes", "y"):
        for module in to_remove:
            modules.remove(module)
        config.save()

    return 0


@cfbs_command("update")
@commit_after_command("Updated module%s", [PLURAL_S])
def update_command(to_update) -> Result:
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    build = config["build"]

    # Update all modules in build if none specified
    to_update = (
        [Module(m) for m in to_update]
        if to_update
        else [Module(m["name"]) for m in build]
    )

    updated = []
    module_updates = ModuleUpdates(config)
    index = None

    for update in to_update:
        old_module = config.get_module_from_build(update.name)
        assert (
            old_module is not None
        ), 'We\'ve already checked that modules are in config["build"]'

        custom_index = old_module is not None and "index" in old_module
        index = Index(old_module["index"]) if custom_index else config.index

        if not old_module:
            index.translate_alias(update)
            old_module = config.get_module_from_build(update.name)

        if not old_module:
            log.warning(
                "old_Module '%s' not in build. Skipping its update." % update.name
            )
            continue

        custom_index = old_module is not None and "index" in old_module
        index = Index(old_module["index"]) if custom_index else config.index

        if not old_module:
            index.translate_alias(update)
            old_module = config.get_module_from_build(update.name)

        if not old_module:
            log.warning("Module '%s' not in build. Skipping its update." % update.name)
            continue

        if "url" in old_module:
            path, commit = clone_url_repo(old_module["url"])
            remote_config = CFBSJson(
                path=path, url=old_module["url"], url_commit=commit
            )

            module_name = old_module["name"]
            provides = remote_config.get_provides()

            if not module_name or module_name not in provides:
                continue

            new_module = provides[module_name]
        else:

            if "version" not in old_module:
                log.warning(
                    "Module '%s' not updatable. Skipping its update."
                    % old_module["name"]
                )
                log.debug("Module '%s' has no version attribute." % old_module["name"])
                continue

            index_info = index.get_module_object(update.name)
            if not index_info:
                log.warning(
                    "Module '%s' not present in the index, cannot update it."
                    % old_module["name"]
                )
                continue

            local_ver = [
                int(version_number)
                for version_number in re.split(r"[-\.]", old_module["version"])
            ]
            index_ver = [
                int(version_number)
                for version_number in re.split(r"[-\.]", index_info["version"])
            ]
            if local_ver == index_ver:
                print("Module '%s' already up to date" % old_module["name"])
                continue
            elif local_ver > index_ver:
                log.warning(
                    "The requested version of module '%s' is older than current version (%s < %s)."
                    " Skipping its update."
                    % (old_module["name"], index_info["version"], old_module["version"])
                )
                continue

            new_module = index_info

        update_module(old_module, new_module, module_updates, update)

        # add new items

        updated.append(update)

    if module_updates.new_deps:
        assert index is not None
        objects = [
            index.get_module_object(d, module_updates.new_deps_added_by[d])
            for d in module_updates.new_deps
        ]
        config.add_with_dependencies(objects)
    config.save()

    if module_updates.changes_made:
        if len(updated) > 1:
            module_updates.msg = (
                "Updated %d modules\n" % len(updated) + module_updates.msg
            )
        else:
            # Remove the '\n - ' part of the message
            module_updates.msg = module_updates.msg[len("\n - ") :]
        print("%s\n" % module_updates.msg)
    else:
        print("Modules are already up to date")

    return Result(
        0, module_updates.changes_made, module_updates.msg, module_updates.files
    )


@cfbs_command("validate")
def validate_command(paths=None, index_arg=None) -> int:
    if paths:
        ret_value = 0

        for path in paths:
            # Exit out early if we find anything wrong like missing files:
            if not os.path.exists(path):
                raise CFBSUserError("Specified path '{}' does not exist".format(path))
            if path.endswith(".json") and not os.path.isfile(path):
                raise CFBSUserError(
                    "'{}' is not a file - Please specify a path to a cfbs project file, ending in .json, or a folder containing a cfbs.json".format(
                        path
                    )
                )
            if not path.endswith(".json") and not os.path.isfile(
                os.path.join(path, "cfbs.json")
            ):
                raise CFBSUserError(
                    "No CFBS project file found at '{}'".format(
                        os.path.join(path, "cfbs.json")
                    )
                )

            # Convert folder to folder/cfbs.json if appropriate:
            if not path.endswith(".json"):
                assert os.path.isdir(path)
                path = os.path.join(path, "cfbs.json")
            assert os.path.isfile(path)

            # Actually open the file and perform validation:
            config = CFBSJson(path=path, index_argument=index_arg)

            r = validate_config(config)
            if r != 0:
                log.warning("Validation of project at path %s failed" % path)
                ret_value = 1
            else:
                print("Successfully validated the project at path", path)

        return ret_value

    if not is_cfbs_repo():
        # TODO change CFBSExitError to CFBSUserError here
        raise CFBSExitError(
            "Cannot validate: this is not a CFBS project. "
            + "Use `cfbs init` to start a new project in this directory, or provide a path to a CFBS project to validate."
        )

    config = CFBSConfig.get_instance()
    return validate_config(config)


def _download_dependencies(
    config, prefer_offline=False, redownload=False, ignore_versions=False
):
    # TODO: This function should be split in 2:
    #       1. Code for downloading things into ~/.cfengine
    #       2. Code for copying things into ./out
    print("\nModules:")
    counter = 1
    max_length = config.longest_module_key_length("name")
    for module in config.get("build", []):
        name = module["name"]
        if name.startswith("./"):
            local_module_copy(module, counter, max_length)
            counter += 1
            continue
        if "commit" not in module:
            raise CFBSExitError("module %s must have a commit property" % name)
        commit = module["commit"]
        if not is_a_commit_hash(commit):
            raise CFBSExitError("'%s' is not a commit reference" % commit)

        url = module.get("url") or module["repo"]
        url = strip_right(url, ".git")
        commit_dir = get_download_path(module)
        if redownload:
            rm(commit_dir, missing_ok=True)
        if "subdirectory" in module:
            module_dir = os.path.join(commit_dir, module["subdirectory"])
        else:
            module_dir = commit_dir
        if not os.path.exists(module_dir):
            if url.endswith(SUPPORTED_ARCHIVES):
                if os.path.exists(commit_dir) and "subdirectory" in module:
                    raise CFBSExitError(
                        "Subdirectory '%s' for module '%s' was not found in fetched archive '%s': "
                        % (module["subdirectory"], name, url)
                        + "Please check cfbs.json for possible typos."
                    )
                fetch_archive(url, commit)
            # a couple of cases where there will not be an archive available:
            # - using an alternate index (index property in module data)
            # - added by URL instead of name (no version property in module data)
            elif "index" in module or "url" in module or ignore_versions:
                if os.path.exists(commit_dir) and "subdirectory" in module:
                    raise CFBSExitError(
                        "Subdirectory '%s' for module '%s' was not found in cloned repository '%s': "
                        % (module["subdirectory"], name, url)
                        + "Please check cfbs.json for possible typos."
                    )
                sh("git clone %s %s" % (url, commit_dir))
                sh("(cd %s && git checkout %s)" % (commit_dir, commit))
            else:
                try:
                    versions = get_json(_VERSION_INDEX)
                except CFBSNetworkError:
                    raise CFBSExitError(
                        "Downloading CFEngine Build Module Index failed - check your Wi-Fi / network settings."
                    )
                try:
                    checksum = versions[name][module["version"]]["archive_sha256"]
                except KeyError:
                    raise CFBSExitError(
                        "Cannot verify checksum of the '%s' module" % name
                    )
                module_archive_url = os.path.join(
                    _MODULES_URL, name, commit + ".tar.gz"
                )
                fetch_archive(
                    module_archive_url, checksum, directory=commit_dir, with_index=False
                )
        target = "out/steps/%03d_%s_%s/" % (counter, module["name"], commit)
        module["_directory"] = target
        module["_counter"] = counter
        subdirectory = module.get("subdirectory", None)
        if not subdirectory:
            cp(commit_dir, target)
        else:
            cp(os.path.join(commit_dir, subdirectory), target)
        print(
            "%03d %s @ %s (Downloaded)" % (counter, pad_right(name, max_length), commit)
        )
        counter += 1


@cfbs_command("download")
def download_command(force, ignore_versions=False) -> int:
    config = CFBSConfig.get_instance()
    r = validate_config(config)
    if r != 0:
        log.warning(
            "At least one error encountered while validating your cfbs.json file."
            + "\nPlease see the error messages above and apply fixes accordingly."
            + "\nIf not fixed, these errors will cause your project to not build in future cfbs versions."
        )
    _download_dependencies(config, redownload=force, ignore_versions=ignore_versions)
    return 0


@cfbs_command("build")
def build_command(ignore_versions=False) -> int:
    config = CFBSConfig.get_instance()
    r = validate_config(config)
    if r != 0:
        log.warning(
            "At least one error encountered while validating your cfbs.json file."
            + "\nPlease see the error messages above and apply fixes accordingly."
            + "\nIf not fixed, these errors will cause your project to not build in future cfbs versions."
        )
        # We want the cfbs build command to be as backwards compatible as possible,
        # so we try building anyway and don't return error(s)
    init_out_folder()
    _download_dependencies(config, prefer_offline=True, ignore_versions=ignore_versions)
    r = perform_build(config)
    return r


@cfbs_command("install")
def install_command(args) -> int:
    if len(args) > 1:
        raise CFBSExitError(
            "Only one destination is allowed for command: cfbs install [destination]"
        )
    if not os.path.exists("out/masterfiles"):
        r = build_command()
        if r != 0:
            return r

    if os.getuid() == 0:
        destination = "/var/cfengine/masterfiles"
    if len(args) > 0:
        destination = args[0]
    elif os.getuid() == 0:
        destination = "/var/cfengine/masterfiles"
    else:
        destination = os.path.join(os.environ["HOME"], ".cfagent/inputs")
    if not destination.startswith("/") and not destination.startswith("./"):
        destination = "./" + destination
    rm(destination, missing_ok=True)
    cp("out/masterfiles", destination)
    print("Installed to %s" % destination)
    return 0


@cfbs_command("help")
def help_command():
    raise CFBSProgrammerError("help_command should not be called, as we use argparse")


def _print_module_info(data):
    ordered_keys = [
        "module",
        "version",
        "status",
        "by",
        "tags",
        "repo",
        "index",
        "commit",
        "subdirectory",
        "dependencies",
        "added_by",
        "description",
    ]
    for key in ordered_keys:
        if key in data:
            if key in ["tags", "dependencies"]:
                value = ", ".join(data[key])
            else:
                value = data[key]
            print("{}: {}".format(key.title().replace("_", " "), value))


@cfbs_command("show")
@cfbs_command("info")
def info_command(modules):
    if not modules:
        raise CFBSExitError(
            "info/show command requires one or more module names as arguments"
        )
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    index = config.index

    build = config.get("build", [])
    assert isinstance(build, list)

    alias = None

    for module in modules:
        print()  # whitespace for readability
        in_build = any(m for m in build if m["name"] == module)
        if not index.exists(module) and not in_build:
            print("Module '{}' does not exist".format(module))
            continue
        if in_build:
            # prefer information from the local source
            data = next(m for m in build if m["name"] == module)
            data["status"] = "Added"
        elif module in index:
            data = index[module]
            if "alias" in data:
                alias = module
                module = data["alias"]
                data = index[module]
            data["status"] = "Added" if in_build else "Not added"
        else:
            if not module.startswith("./"):
                module = "./" + module
            data = next((m for m in build if m["name"] == module), None)
            if data is None:
                print("Path {} exists but is not yet added as a module.".format(module))
                continue
            data["status"] = "Added"
        data["module"] = (module + "({})".format(alias)) if alias else module
        _print_module_info(data)
    print()  # extra line for ease of reading
    return 0


@cfbs_command("analyze")
@cfbs_command("analyse")
def analyze_command(
    policyset_paths,
    json_filename=None,
    reference_version=None,
    masterfiles_dir=None,
    user_ignored_path_components=None,
    offline=False,
    verbose=False,
) -> int:
    if len(policyset_paths) == 0:
        # no policyset path is a shorthand for using the current directory as the policyset path
        log.info(
            "No path was provided. Using the current directory as the policy set path."
        )
        path = "."
    else:
        # currently, only support analyzing only one path
        path = policyset_paths[0]

        if len(policyset_paths) > 1:
            log.warning(
                "More than one path to analyze provided. Analyzing the first one and ignoring the others."
            )

    if not os.path.isdir(path):
        raise CFBSExitError("the provided policy set path is not a directory")

    if masterfiles_dir is None:
        masterfiles_dir = "masterfiles"
    # override masterfiles directory name (e.g. "inputs")
    # strip trailing path separators
    masterfiles_dir = masterfiles_dir.rstrip(os.sep)
    # we assume the modules directory is always called "modules"
    # thus `masterfiles_dir` can't be set to "modules"
    if masterfiles_dir == "modules":
        log.warning(
            'The masterfiles directory cannot be named "modules". Using the name "masterfiles" instead.'
        )
        masterfiles_dir = "masterfiles"

    # the policyset path can either contain only masterfiles (masterfiles-path), or contain folders containing modules and masterfiles (parent-path)
    # try to automatically determine which one it is (by checking whether `path` contains `masterfiles_dir`)
    is_parentpath = os.path.isdir(os.path.join(path, masterfiles_dir))

    print("Policy set path:", path, "\n")

    analyzed_files, versions_data = analyze_policyset(
        path,
        is_parentpath,
        reference_version,
        masterfiles_dir,
        user_ignored_path_components,
        offline,
    )

    versions_data.display(verbose)
    analyzed_files.display()

    if json_filename is not None:
        json_dict = OrderedDict()

        json_dict["policy_set_path"] = path
        json_dict["versions_data"] = versions_data.to_json_dict()
        json_dict["analyzed_files"] = analyzed_files.to_json_dict()

        write_json(json_filename + ".json", json_dict)

    return 0


@cfbs_command("input")
@commit_after_command("Added input for module%s", [PLURAL_S])
def input_command(args, input_from="cfbs input") -> Result:
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    do_commit = False
    files_to_commit = []
    for module_name in args:
        module = config.get_module_from_build(module_name)
        if not module:
            print("Skipping module '%s', module not found" % module_name)
            continue
        if "input" not in module:
            print("Skipping module '%s', no input needed" % module_name)
            continue

        input_path = os.path.join(".", module_name, "input.json")
        if os.path.isfile(input_path):
            if prompt_user(
                config.non_interactive,
                "Input already exists for this module, do you want to overwrite it?",
                YES_NO_CHOICES,
                "no",
            ).lower() in ("no", "n"):
                continue

        input_data = copy.deepcopy(module["input"])
        config.input_command(module_name, input_data)

        write_json(input_path, input_data)
        do_commit = True
        files_to_commit.append(input_path)
    config.save()
    return Result(0, do_commit, None, files_to_commit)


@cfbs_command("set-input")
@commit_after_command("Set input for module %s", [FIRST_ARG])
def set_input_command(name, infile):
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    module = config.get_module_from_build(name)
    if module is None:
        log.error("Module '%s' not found" % name)
        return 1

    spec = module.get("input")
    if spec is None:
        log.error("Module '%s' does not accept input" % name)
        return 1
    log.debug("Input spec for module '%s': %s" % (name, pretty(spec)))

    try:
        data = json.load(infile, object_pairs_hook=OrderedDict)
    except json.decoder.JSONDecodeError as e:
        log.error("Error reading json from stdin: %s" % e)
        return 1
    log.debug("Input data for module '%s': %s" % (name, pretty(data)))

    def _compare_dict(a, b, ignore=None):
        assert isinstance(a, dict) and isinstance(b, dict)
        ignore = ignore or set()
        if set(a.keys()) != set(b.keys()) - ignore:
            return False
        # Avoid code duplication by converting the values of the two dicts
        # into two lists in the same order and compare the lists instead
        keys = a.keys()
        return _compare_list([a[key] for key in keys], [b[key] for key in keys])

    def _compare_list(a, b):
        assert isinstance(a, list) and isinstance(b, list)
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            if type(x) is not type(y):
                return False
            if isinstance(x, dict):
                if not _compare_dict(x, y):
                    return False
            elif isinstance(x, list):
                if not _compare_list(x, y):
                    return False
            else:
                assert x is None or isinstance(
                    x, (int, float, str, bool)
                ), "Illegal value type"
                if x != y:
                    return False
        return True

    for a, b in zip(spec, data):
        if (
            not isinstance(a, dict)
            or not isinstance(b, dict)
            or not _compare_dict(a, b, ignore=set({"response"}))
        ):
            log.error(
                "Input data for module '%s' does not conform with input definition"
                % name
            )
            return 1

    path = os.path.join(name, "input.json")

    log.debug("Comparing with data already in file '%s'" % path)
    old_data = read_json(path)
    changes_made = old_data != data

    if changes_made:
        write_json(path, data)
        log.debug(
            "Input data for '%s' changed, writing json to file '%s'" % (name, path)
        )
    else:
        log.debug("Input data for '%s' unchanged, nothing to write / commit" % name)

    return Result(0, changes_made, None, [path])


@cfbs_command("get-input")
def get_input_command(name, outfile) -> int:
    config = CFBSConfig.get_instance()
    config.warn_about_unknown_keys()
    module = config.get_module_from_build(name)
    if module is None:
        module = config.index.get_module_object(name)
    if module is None:
        log.error("Module '%s' not found" % name)
        return 1

    if "input" not in module:
        data = []
    else:
        path = os.path.join(name, "input.json")
        data = read_json(path)
        if data is None:
            log.debug("Loaded input from module '%s' definition" % name)
            data = module["input"]
        else:
            log.debug("Loaded input from '%s'" % path)

    data = pretty(data) + "\n"
    try:
        outfile.write(data)
    except OSError as e:
        log.error("Failed to write json: %s" % e)
        return 1
    return 0


@cfbs_command("generate-release-information")
def generate_release_information_command(
    omit_download=False, check=False, min_version=None
):
    generate_release_information(omit_download, check, min_version)
    return 0

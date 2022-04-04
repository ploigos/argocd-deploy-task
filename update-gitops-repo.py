import os
import sh
import sys
import re

GIT_REPO_REGEX = re.compile(r"(?P<protocol>^https:\/\/|^http:\/\/)?(?P<address>.*$)")


def clone_repo( # pylint: disable=too-many-arguments
    repo_dir,
    repo_url,
    repo_branch,
    git_email,
    git_name,
    username=None,
    password=None
):
    """Clones and checks out the deployment configuration repository.

    Parameters
    ----------
    repo_dir : str
        Path to where to clone the repository
    repo_uri : str
        URI of the repository to clone.
    git_email : str
        email to use when performing git operations in the cloned repository
    git_name : str
        name to use when performing git operations in the cloned repository

    Returns
    -------
    str
        Path to the directory where the deployment configuration repository was cloned
        and checked out.

    Raises
    ------
    StepRunnerException
    * if error cloning repository
    * if error checking out branch of repository
    * if error configuring repo user
    """
    repo_match = GIT_REPO_REGEX.match(repo_url)
    repo_protocol = repo_match.groupdict()['protocol']
    repo_address = repo_match.groupdict()['address']
    # if deployment config repo uses http/https push using user/pass
    # else push using ssh
    if username and password and repo_protocol and re.match(
            r'^http://|^https://',
            repo_protocol
    ):
        repo_url_with_auth = \
            f"{repo_protocol}{username}:{password}" \
            f"@{repo_address}"
    else:
        repo_url_with_auth = repo_url
    try:
        sh.git.clone( # pylint: disable=no-member
            repo_url_with_auth,
            repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        raise RuntimeError(f"Error cloning repository ({repo_url}): {error}") from error

    try:
        # no atomic way in git to checkout out new or existing branch,
        # so first try to check out existing, if that doesn't work try new
        try:
            sh.git.checkout(  # pylint: disable=no-member
                repo_branch,
                _cwd=repo_dir,
                _out=sys.stdout,
                _err=sys.stderr
            )
        except sh.ErrorReturnCode:
            sh.git.checkout(
                '-b',
                repo_branch,
                _cwd=repo_dir,
                _out=sys.stdout,
                _err=sys.stderr
            )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise RuntimeError(
            f"Unexpected error checking out new or existing branch ({repo_branch}) from repository ({repo_url}): {error}"
        ) from error

    try:
        sh.git.config( # pylint: disable=no-member
            'user.email',
            git_email,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
        sh.git.config( # pylint: disable=no-member
            'user.name',
            git_name,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise RuntimeError(
            f"Unexpected error configuring git user.email ({git_email})"
            f" and user.name ({git_name}) for repository ({repo_url})"
            f" in directory ({repo_dir}): {error}"
        ) from error

    return repo_dir


def _update_yaml_file_value(work_dir_path, file, yq_path, value):
    # Use the yq command to update the file
    try:
        sh.yq.eval( # pylint: disable=no-member
            f'{yq_path} = "{value}"',
            file,
            '--inplace'
        )
    except sh.ErrorReturnCode as error:
        raise RuntimeError(
            f"Error updating YAML file ({file}) target ({yq_path}) with value ({value}):"
            f" {error}"
        ) from error

    return file


def _git_commit_file(git_commit_message, file_path, repo_dir):
    try:
        sh.git.add( # pylint: disable=no-member
            file_path,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise RuntimeError(
            f"Unexpected error adding file ({file_path}) to commit"
            f" in git repository ({repo_dir}): {error}"
        ) from error

    try:
        sh.git.commit( # pylint: disable=no-member
            '--allow-empty',
            '--all',
            '--message', git_commit_message,
            _cwd=repo_dir,
            _out=sys.stdout,
            _err=sys.stderr
        )
    except sh.ErrorReturnCode as error:
        # NOTE: this should never happen
        raise RuntimeError(
            f"Unexpected error commiting file ({file_path})"
            f" in git repository ({repo_dir}): {error}"
        ) from error


def _git_push(repo_dir, url=None):
    """
    Raises string if error pushing commits
    """

    git_push = sh.git.push.bake(url) if url else sh.git.push

    # push commits
    try:
        git_push(
            _cwd=repo_dir,
            _out=sys.stdout
        )
    except sh.ErrorReturnCode as error:
        raise RuntimeError(
            f"Error pushing commits from repository directory ({repo_dir}) to"
            f" repository ({url}): {error}"
        ) from error


def _git_push_deployment_config_repo(
        deployment_config_repo,
        deployment_config_repo_dir,
        username,
        password
):
    deployment_config_repo_match = GIT_REPO_REGEX.match(deployment_config_repo)
    deployment_config_repo_protocol = deployment_config_repo_match.groupdict()['protocol']
    deployment_config_repo_address = deployment_config_repo_match.groupdict()['address']

    # if deployment config repo uses http/https push using user/pass
    # else push using ssh
    if deployment_config_repo_protocol and re.match(
            r'^http://|^https://',
            deployment_config_repo_protocol
    ):
        deployment_config_repo_with_user_pass = \
            f"{deployment_config_repo_protocol}{username}:{password}" \
            f"@{deployment_config_repo_address}"
        _git_push(
            repo_dir=deployment_config_repo_dir,
            url=deployment_config_repo_with_user_pass
        )
    else:
        _git_push(
            repo_dir=deployment_config_repo_dir
        )


def deploy(git_password, container_image_address):  # pylint: disable=too-many-locals, too-many-statements

    results = {}

    # get input
    deployment_config_repo = 'https://github.com/dwinchell-robot/reference-quarkus-mvn-cloud-resources_tekton_workflow-minimal.git'
    deployment_config_repo_branch = 'main'
    target_file = 'charts/reference-quarkus-mvn-deploy/values-DEV.yaml'
    deployment_config_helm_chart_values_file_container_image_address_yq_path = '.image.tag'

    git_email = 'tekton@example.com'
    git_name = 'Tekton'
    git_username = 'dwinchell-robot'

    work_dir_path = '.'

    try:

        # clone the configuration repository
        print("Clone the configuration repository")
        repo_dir = os.path.join(work_dir_path, 'deployment-config-repo')
        os.makedirs(repo_dir, exist_ok=True)
        deployment_config_repo_dir = clone_repo(
            repo_dir= repo_dir,
            repo_url=deployment_config_repo,
            repo_branch=deployment_config_repo_branch,
            git_email = git_email,
            git_name= git_name,
            username = git_username,
            password = git_password
        )

        # update values file, commit it, push it, and tag it
        print("Update the environment values file")
        deployment_config_helm_chart_environment_values_file_path = os.path.join(
            deployment_config_repo_dir,
            target_file
        )
        _update_yaml_file_value(
            work_dir_path = work_dir_path,
            file=deployment_config_helm_chart_environment_values_file_path,
            yq_path=deployment_config_helm_chart_values_file_container_image_address_yq_path,
            value=container_image_address
        )

        print("Commit the updated environment values file")
        _git_commit_file(
            git_commit_message=f'Updating values for deployment',
            file_path=target_file,
            repo_dir=deployment_config_repo_dir
        )
        print("Push the updated environment values file")
        _git_push_deployment_config_repo(
            deployment_config_repo=deployment_config_repo,
            deployment_config_repo_dir=deployment_config_repo_dir,
            username=git_username,
            password=git_password
        )
        # TODO: capture pushed commit hash in results

    except RuntimeError as error:
        results['success'] = False
        results['message'] = f"Error updating gitops repository {str(error)}"

    return results


# Run the script
task_results = deploy(
    git_password = sys.argv[1],
    container_image_address = sys.argv[2]
)
print(task_results)

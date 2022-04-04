import os
import sh
import sys
import re
from io import StringIO
import yaml
from pathlib import Path


GIT_REPO_REGEX = re.compile(r"(?P<protocol>^https:\/\/|^http:\/\/)?(?P<address>.*$)")
ARGOCD_OP_IN_PROGRESS_REGEX = re.compile(
    r'.*FailedPrecondition.*another\s+operation\s+is\s+already\s+in\s+progress',
    re.DOTALL
)
ARGOCD_HEALTH_STATE_TRANSITIONED_FROM_HEALTHY_TO_DEGRADED = re.compile(
    r".*level=fatal.*health\s+state\s+has\s+transitioned\s+from\s.+\s+to\s+Degraded",
    re.DOTALL
)
MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_OP_RETRIES = 2
MAX_ATTEMPT_TO_WAIT_FOR_ARGOCD_HEALTH_RETRIES = 2


def create_working_dir_sub_dir(work_dir_path, sub_dir_relative_path=""):
    """Create a folder under the working/stepname folder.

    Returns
    -------
    str
        Path to created working sub directory.
    """
    file_path = os.path.join(work_dir_path, sub_dir_relative_path)
    os.makedirs(file_path, exist_ok=True)
    return file_path


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
        raise f"Error cloning repository ({repo_url}): {error}"

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
        raise f"Unexpected error checking out new or existing branch ({repo_branch}) from repository ({repo_url}): {error}"

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
        raise (
            f"Unexpected error configuring git user.email ({git_email})"
            f" and user.name ({git_name}) for repository ({repo_url})"
            f" in directory ({repo_dir}): {error}"
        )

    return repo_dir


def write_working_file(work_dir_path, filename, contents=None):
    """Write content or touch filename in working directory for this step.

    Parameters
    ----------
    filename : str
        File name to create
    contents : str, optional
        Contents to write to the file

    Returns
    -------
    str
        Return a string to the file path
    """
    # eg: step-runner-working/step_name
    file_path = os.path.join(work_dir_path, filename)

    # sub-directories might be passed filename, eg: foo/filename
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if contents is None:
        Path(file_path).touch()
    else:
        with open(file_path, 'wb') as file:
            file.write(contents)
    return file_path


def _update_yaml_file_value(work_dir_path, file, yq_path, value):
    """Update a YAML file using YQ.

    Parameters
    ----------
    file : str
        Path to file to update.
    yq_path : str
        YQ path to the value to update.
    value: str
        value to update the `yq_path`

    Returns
    -------
    str
        Path to the file to update.

    Raises
    ------
    StepRunnerException
        If error updating file.
    """
    # NOTE: use a YQ script to update so that comment can be injected
    # TODO: update to YQ 4 and drop script file (and associated python parameters)

    # inplace update the file
    try:
        sh.yq.eval( # pylint: disable=no-member
            f'{yq_path} = "{value}"',
            file,
            '--inplace'
        )
    except sh.ErrorReturnCode as error:
        raise (
            f"Error updating YAML file ({file}) target ({yq_path}) with value ({value}):"
            f" {error}"
        )

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
        raise (
            f"Unexpected error adding file ({file_path}) to commit"
            f" in git repository ({repo_dir}): {error}"
        )

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
        raise (
            f"Error pushing commits from repository directory ({repo_dir}) to"
            f" repository ({url}): {error}"
        )


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
    deployment_config_helm_chart_path = 'charts/reference-quarkus-mvn-deploy'
    deployment_config_destination_cluster_uri = 'https://kubernetes.default.svc'
    deployment_config_destination_cluster_token = '' # self.get_value('kube-api-token')
    deployment_config_helm_chart_environment_values_file = 'values-DEV.yaml'
    deployment_config_helm_chart_values_file_container_image_address_yq_path = '.image.tag'
    deployment_config_helm_chart_additional_value_files = ''
    additional_helm_values_files = ''
    argocd_app_name = 'tekton-task-app'
    # container_image_address = 'myimage:newsha' # Function parameter

    git_email = 'tektondeploytask@example.com'
    git_name = 'Tekton Deploy Task'
    git_username = 'dwinchell-robot'
    # git_password = git_password // Function argument

    environment = 'DEV'

    work_dir_path = '.'

    results['container-image-deployed-address'] = container_image_address

    try:

        # clone the configuration repository
        print("Clone the configuration repository")
        repo_dir = create_working_dir_sub_dir('.', 'deployment-config-repo')
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
            deployment_config_helm_chart_path,
            deployment_config_helm_chart_environment_values_file
        )
        _update_yaml_file_value(
            work_dir_path = work_dir_path,
            file=deployment_config_helm_chart_environment_values_file_path,
            yq_path=deployment_config_helm_chart_values_file_container_image_address_yq_path,
            value=container_image_address
        )

        print("Commit the updated environment values file")
        _git_commit_file(
            git_commit_message=f'Updating values for deployment to {environment}',
            file_path=os.path.join(
                deployment_config_helm_chart_path,
                deployment_config_helm_chart_environment_values_file
            ),
            repo_dir=deployment_config_repo_dir
        )
        print("Push the updated environment values file")
        deployment_config_repo_tag = 'DO NOT USE'
        _git_push_deployment_config_repo(
            deployment_config_repo=deployment_config_repo,
            deployment_config_repo_dir=deployment_config_repo_dir,
            username=git_username,
            password=git_password
        )
        # TODO: capture pushed commit hash in results

    except RuntimeError as error:
        results['success'] = False
        results['message'] = f"Error deploying to environment ({environment}):" \
                              f" {str(error)}"

    return results


# Run the script
task_results = deploy(
    git_password = sys.argv[1],
    container_image_address = sys.argv[2]
)
print(task_results)

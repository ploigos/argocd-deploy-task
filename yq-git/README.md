# yq-git Tekton Task

1. Build the Task image and yaml.
```
buildah bud -t yq-git-ubi8
podman push yq-git-ubi8 quay.io/dwinchell_redhat/yq-git-ubi8
./build.sh
./test.sh
```

2. Configure git authentication. For a detailed explanation, see the [Tekton documentation for authentication with git repositories](https://tekton.dev/docs/pipelines/auth/).
   1. Create a secret with the git credentials. This example assumes you are using HTTPS with basic authentication.
    ```shell
    oc create secret generic ops-repo-auth \
        --from-literal=username=<user_name> \
        --from-literal=password=<password> \
        --type=kubernetes.io/basic-auth

    oc edit secret ops-repo-auth
    ```
   2. Edit the secret to add the tekton.dev/git-0 annotation with the host of your git repository.
   Example:
    ```
    apiVersion: v1
    data:
      password: <redacted>
      username: <redacted>
    kind: Secret
    metadata:
      creationTimestamp: "2022-04-07T16:21:22Z"
      name: ops-repo-auth
      namespace: devsecops
      resourceVersion: "1972420"
      uid: 804b6445-8f4c-4f92-87ae-f5a4dbc83196
      annotations:                                     # ADD THIS
           tekton.dev/git-0: https://github.com        # ADD THIS
    type: kubernetes.io/basic-auth
    ```
   iii. Link the secret to the service account being used to run the task
   ```shell
    oc secret link pipeline ops-repo-auth
    ```

(faq:troubleshooting)=

# Troubleshooting

When troubleshooting, you may see unexpected behaviors or receive an error
message. This section provides links for identifying the cause of the
problem and how to resolve it.

## Behavior

### JupyterHub proxy fails to start

If you have tried to start the JupyterHub proxy and it fails to start:

- check if the JupyterHub IP configuration setting is
  `c.JupyterHub.ip = '*'`; if it is, try `c.JupyterHub.ip = ''`
- Try starting with `jupyterhub --ip=0.0.0.0`

**Note**: If this occurs on Ubuntu/Debian, check that you are using a
recent version of [Node](https://nodejs.org). Some versions of Ubuntu/Debian come with a very old version
of Node and it is necessary to update Node.

### sudospawner fails to run

If the sudospawner script is not found in the path, sudospawner will not run.
To avoid this, specify sudospawner's absolute path. For example, start
jupyterhub with:

    jupyterhub --SudoSpawner.sudospawner_path='/absolute/path/to/sudospawner'

or add:

    c.SudoSpawner.sudospawner_path = '/absolute/path/to/sudospawner'

to the config file, `jupyterhub_config.py`.

### What is the default behavior when none of the lists (admin, allowed, allowed groups) are set?

When nothing is given for these lists, there will be no admins, and all users
who can authenticate on the system (i.e. all the Unix users on the server with
a password) will be allowed to start a server. The allowed username set lets you limit
this to a particular set of users, and admin_users lets you specify who
among them may use the admin interface (not necessary, unless you need to do
things like inspect other users' servers or modify the user list at runtime).

### JupyterHub Docker container is not accessible at localhost

Even though the command to start your Docker container exposes port 8000
(`docker run -p 8000:8000 -d --name jupyterhub quay.io/jupyterhub/jupyterhub jupyterhub`),
it is possible that the IP address itself is not accessible/visible. As a result,
when you try http://localhost:8000 in your browser, you are unable to connect
even though the container is running properly. One workaround is to explicitly
tell Jupyterhub to start at `0.0.0.0` which is visible to everyone. Try this
command:
`docker run -p 8000:8000 -d --name jupyterhub quay.io/jupyterhub/jupyterhub jupyterhub --ip 0.0.0.0 --port 8000`

### How can I kill ports from JupyterHub-managed services that have been orphaned?

I started JupyterHub + nbgrader on the same host without containers. When I try to restart JupyterHub + nbgrader with this configuration, errors appear that the service accounts cannot start because the ports are being used.

How can I kill the processes that are using these ports?

Run the following command:

    sudo kill -9 $(sudo lsof -t -i:<service_port>)

Where `<service_port>` is the port used by the nbgrader course service. This configuration is specified in `jupyterhub_config.py`.

### Why am I getting a Spawn failed error message?

After successfully logging in to JupyterHub with a compatible authenticator, I get a 'Spawn failed' error message in the browser. The JupyterHub logs have `jupyterhub KeyError: "getpwnam(): name not found: <my_user_name>`.

This issue occurs when the authenticator requires a local system user to exist. In these cases, you need to use a spawner
that does not require an existing system user account, such as `DockerSpawner` or `KubeSpawner`.

### How can I run JupyterHub with sudo but use my current environment variables and virtualenv location?

When launching JupyterHub with `sudo jupyterhub` I get import errors and my environment variables don't work.

When launching services with `sudo ...` the shell won't have the same environment variables or `PATH`s in place. The most direct way to solve this issue is to use the full path to your python environment and add environment variables. For example:

```bash
sudo MY_ENV=abc123 \
  /home/foo/venv/bin/python3 \
  /srv/jupyterhub/jupyterhub
```

## Errors

### Error 500 after spawning my single-user server

You receive a 500 error while accessing the URL `/user/<your_name>/...`.
This is often seen when your single-user server cannot verify your user cookie
with the Hub.

There are two likely reasons for this:

1. The single-user server cannot connect to the Hub's API (networking
   configuration problems)
2. The single-user server cannot _authenticate_ its requests (invalid token)

#### Symptoms

The main symptom is a failure to load _any_ page served by the single-user
server, met with a 500 error. This is typically the first page at `/user/<your_name>`
after logging in or clicking "Start my server". When a single-user notebook server
receives a request, the notebook server makes an API request to the Hub to
check if the cookie corresponds to the right user. This request is logged.

If everything is working, the response logged will be similar to this:

```
200 GET /hub/api/authorizations/cookie/jupyterhub-token-name/[secret] (@10.0.1.4) 6.10ms
```

You should see a similar 200 message, as above, in the Hub log when you first
visit your single-user notebook server. If you don't see this message in the log, it
may mean that your single-user notebook server is not connecting to your Hub.

If you see 403 (forbidden) like this, it is likely a token problem:

```
403 GET /hub/api/authorizations/cookie/jupyterhub-token-name/[secret] (@10.0.1.4) 4.14ms
```

Check the logs of the single-user notebook server, which may have more detailed
information on the cause.

#### Causes and resolutions

##### No authorization request

If you make an API request and it is not received by the server, you likely
have a network configuration issue. Often, this happens when the Hub is only
listening on 127.0.0.1 (default) and the single-user servers are not on the
same 'machine' (can be physically remote, or in a docker container or VM). The
fix for this case is to make sure that `c.JupyterHub.hub_ip` is an address
that all single-user servers can connect to, e.g.:

```python
c.JupyterHub.hub_ip = '10.0.0.1'
```

##### 403 GET /hub/api/authorizations/cookie

If you receive a 403 error, the API token for the single-user server is likely
invalid. Commonly, the 403 error is caused by resetting the JupyterHub
database (either removing jupyterhub.sqlite or some other action) while
leaving single-user servers running. This happens most frequently when using
DockerSpawner because Docker's default behavior is to stop/start containers
that reset the JupyterHub database, rather than destroying and recreating
the container every time. This means that the same API token is used by the
server for its whole life until the container is rebuilt.

The fix for this Docker case is to remove any Docker containers seeing this
issue (typically all containers created before a certain point in time):

    docker rm -f jupyter-name

After this, when you start your server via JupyterHub, it will build a
new container. If this was the underlying cause of the issue, you should see
your server again.

##### Proxy settings (403 GET)

When your whole JupyterHub sits behind an organization proxy (_not_ a reverse proxy like NGINX as part of your setup and _not_ the configurable-http-proxy) the environment variables `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, and `https_proxy` might be set. This confuses the JupyterHub single-user servers: When connecting to the Hub for authorization they connect via the proxy instead of directly connecting to the Hub on localhost. The proxy might deny the request (403 GET). This results in the single-user server thinking it has the wrong auth token. To circumvent this you should add `<hub_url>,<hub_ip>,localhost,127.0.0.1` to the environment variables `NO_PROXY` and `no_proxy`.

### Launching Jupyter Notebooks to run as an externally managed JupyterHub service with the `jupyterhub-singleuser` command returns a `JUPYTERHUB_API_TOKEN` error

{ref}`services-reference` allow processes to interact with JupyterHub's REST API. Example use-cases include:

- **Secure Testing**: provide a canonical Jupyter Notebook for testing production data to reduce the number of entry points into production systems.
- **Grading Assignments**: provide access to shared Jupyter Notebooks that may be used for management tasks such as grading assignments.
- **Private Dashboards**: share dashboards with certain group members.

If possible, try to run the Jupyter Notebook as an externally managed service with one of the provided [jupyter/docker-stacks](https://github.com/jupyter/docker-stacks).

Standard JupyterHub installations include a [jupyterhub-singleuser](https://github.com/jupyterhub/jupyterhub/blob/9fdab027daa32c9017845572ad9d5ba1722dbc53/setup.py#L116) command which is built from the `jupyterhub.singleuser:main` method. The `jupyterhub-singleuser` command is the default command when JupyterHub launches single-user Jupyter Notebooks. One of the goals of this command is to make sure the version of JupyterHub installed within the Jupyter Notebook coincides with the version of the JupyterHub server itself.

If you launch a Jupyter Notebook with the `jupyterhub-singleuser` command directly from the command line, the Jupyter Notebook won't have access to the `JUPYTERHUB_API_TOKEN` and will return:

```
    JUPYTERHUB_API_TOKEN env is required to run jupyterhub-singleuser.
    Did you launch it manually?
```

If you plan on testing `jupyterhub-singleuser` independently from JupyterHub, then you can set the API token environment variable. For example, if you were to run the single-user Jupyter Notebook on the host, then:

    export JUPYTERHUB_API_TOKEN=my_secret_token
    jupyterhub-singleuser

With a docker container, pass in the environment variable with the run command:

    docker run -d \
      -p 8888:8888 \
      -e JUPYTERHUB_API_TOKEN=my_secret_token \
      jupyter/datascience-notebook:latest

[This example](https://github.com/jupyterhub/jupyterhub/tree/HEAD/examples/service-notebook/external) demonstrates how to combine the use of the `jupyterhub-singleuser` environment variables when launching a Notebook as an externally managed service.

### Jupyter Notebook/Lab can be launched, but notebooks seem to hang when trying to execute a cell

This often occurs when your browser is unable to open a websocket connection to a Jupyter kernel.

#### Diagnose

Open your browser console, e.g. [Chrome](https://developer.chrome.com/docs/devtools/console), [Firefox](https://firefox-source-docs.mozilla.org/devtools-user/web_console/).
If you see errors related to opening websockets this is likely to be the problem.

#### Solutions

This could be caused by anything related to the network between your computer/browser and the server running JupyterHub, such as:

- reverse proxies (see {ref}`howto:config:reverse-proxy` for example configurations)
- anti-virus or firewalls running on your computer or JupyterHub server
- transparent proxies running on your network

## How do I...?

### Use a chained SSL certificate

Some certificate providers, i.e. Entrust, may provide you with a chained
certificate that contains multiple files. If you are using a chained
certificate you will need to concatenate the individual files by appending the
chained cert and root cert to your host cert:

    cat your_host.crt chain.crt root.crt > your_host-chained.crt

You would then set in your `jupyterhub_config.py` file the `ssl_key` and
`ssl_cert` as follows:

    c.JupyterHub.ssl_cert = your_host-chained.crt
    c.JupyterHub.ssl_key = your_host.key

#### Example

Your certificate provider gives you the following files: `example_host.crt`,
`Entrust_L1Kroot.txt`, and `Entrust_Root.txt`.

Concatenate the files appending the chain cert and root cert to your host cert:

    cat example_host.crt Entrust_L1Kroot.txt Entrust_Root.txt > example_host-chained.crt

You would then use the `example_host-chained.crt` as the value for
JupyterHub's `ssl_cert`. You may pass this value as a command line option
when starting JupyterHub or more conveniently set the `ssl_cert` variable in
JupyterHub's configuration file, `jupyterhub_config.py`. In `jupyterhub_config.py`,
set:

    c.JupyterHub.ssl_cert = /path/to/example_host-chained.crt
    c.JupyterHub.ssl_key = /path/to/example_host.key

where `ssl_cert` is example-chained.crt and ssl_key to your private key.

Then restart JupyterHub.

See also {ref}`ssl-encryption`.

### Install JupyterHub without a network connection

Both conda and pip can be used without a network connection. You can make your
own repository (directory) of conda packages and/or wheels, and then install
from there instead of the internet.

For instance, you can install JupyterHub with pip and configurable-http-proxy
with npmbox:

    python3 -m pip wheel jupyterhub
    npmbox configurable-http-proxy

### I want access to the whole filesystem and still default users to their home directory

Setting the following in `jupyterhub_config.py` will configure access to
the entire filesystem and set the default to the user's home directory.

    c.Spawner.notebook_dir = '/'
    c.Spawner.default_url = '/home/%U' # %U will be replaced with the username

### How do I use JupyterLab's pre-release version with JupyterHub?

While JupyterLab is still under active development, we have had users
ask about how to try out JupyterLab with JupyterHub.

You need to install and enable the JupyterLab extension system-wide,
then you can change the default URL to `/lab`.

For instance:

    python3 -m pip install jupyterlab
    jupyter serverextension enable --py jupyterlab --sys-prefix

The important thing is that JupyterLab is installed and enabled in the
single-user notebook server environment. For system users, this means
system-wide, as indicated above. For Docker containers, it means inside
the single-user docker image, etc.

In `jupyterhub_config.py`, configure the Spawner to tell the single-user
notebook servers to default to JupyterLab:

    c.Spawner.default_url = '/lab'

### How do I set up JupyterHub for a workshop (when users are not known ahead of time)?

1. Set up JupyterHub using OAuthenticator for GitHub authentication
2. Configure the admin list to have workshop leaders listed with administrator privileges.

Users will need a GitHub account to log in and be authenticated by the Hub.

### I'm seeing "403 Forbidden XSRF cookie does not match POST" when users try to login

During login, JupyterHub takes the request IP into account for CSRF protection.
If proxies are not configured to properly set forwarded ips,
JupyterHub will see all requests as coming from an internal ip,
likely the ip of the proxy itself.
You can see this in the JupyterHub logs, which log the ip address of requests.
If most requests look like they are coming from a small number `10.0.x.x` or `172.16.x.x` ips, the proxy is not forwarding the true request ip properly.
If the proxy has multiple replicas,
then it is likely the ip may change from one request to the next,
leading to this error during login:

> 403 Forbidden XSRF cookie does not match POST argument

The best way to fix this is to ensure your proxies set the forwarded headers, e.g. for nginx:

```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

But if this is not available to you, you can instruct jupyterhub to ignore IPs from certain networks
with the environment variable `$JUPYTERHUB_XSRF_ANONYMOUS_IP_CIDRS`.
For example, to ignore the common [private networks](https://en.wikipedia.org/wiki/Private_network#Private_IPv4_addresses):

```bash
export JUPYTERHUB_XSRF_ANONYMOUS_IP_CIDRS="10.0.0.0/8;172.16.0.0/12;192.168.0.0/16"
```

The result will be that any request from an ip on one of these networks will be treated as coming from the same source.

To totally disable taking the ip into consideration, set

```bash
export JUPYTERHUB_XSRF_ANONYMOUS_IP_CIDRS="0.0.0.0/0"
```

If your proxy sets its own headers to identify a browser origin, you can instruct JupyterHub to use those:

```bash
export JUPYTERHUB_XSRF_ANONYMOUS_ID_HEADERS="My-Custom-Header;User-Agent"
```

Again, these things are only used to compute the XSRF token used while a user is not logged in (i.e. during login itself).

### How do I set up rotating daily logs?

You can do this with [logrotate](https://linux.die.net/man/8/logrotate),
or pipe to `logger` to use Syslog instead of directly to a file.

For example, with this logrotate config file:

```
/var/log/jupyterhub.log {
  copytruncate
  daily
}
```

and run this daily by putting a script in `/etc/cron.daily/`:

```bash
logrotate /path/to/above-config
```

Or use syslog:

    jupyterhub | logger -t jupyterhub

### Toree integration with HDFS rack awareness script

The Apache Toree kernel will have an issue when running with JupyterHub if the standard HDFS rack awareness script is used. This will materialize in the logs as a repeated WARN:

```bash
16/11/29 16:24:20 WARN ScriptBasedMapping: Exception running /etc/hadoop/conf/topology_script.py some.ip.address
ExitCodeException exitCode=1:   File "/etc/hadoop/conf/topology_script.py", line 63
    print rack
             ^
SyntaxError: Missing parentheses in call to 'print'

    at `org.apache.hadoop.util.Shell.runCommand(Shell.java:576)`
```

In order to resolve this issue, there are two potential options.

1. Update HDFS core-site.xml, so the parameter "net.topology.script.file.name" points to a custom
   script (e.g. /etc/hadoop/conf/custom_topology_script.py). Copy the original script and change the first line point
   to a python two installation (e.g. /usr/bin/python).
2. In spark-env.sh add a Python 2 installation to your path (e.g. export PATH=/opt/anaconda2/bin:$PATH).

### Where do I find Docker images and Dockerfiles related to JupyterHub?

Docker images can be found at the [JupyterHub organization on Quay.io](https://quay.io/organization/jupyterhub).
The Docker image [jupyterhub/singleuser](https://quay.io/repository/jupyterhub/singleuser)
provides an example single-user notebook server for use with DockerSpawner.

Additional single-user notebook server images can be found at the [Jupyter
organization on Quay.io](https://quay.io/organization/jupyter) and information
about each image at the [jupyter/docker-stacks repo](https://github.com/jupyter/docker-stacks).

### How can I view the logs for JupyterHub or the user's Notebook servers when using the DockerSpawner?

Use `docker logs <container>` where `<container>` is the container name defined within `docker-compose.yml`. For example, to view the logs of the JupyterHub container use:

    docker logs hub

By default, the user's notebook server is named `jupyter-<username>` where `username` is the user's username within JupyterHub's database.
So if you wanted to see the logs for user `foo` you would use:

    docker logs jupyter-foo

You can also tail logs to view them in real-time using the `-f` option:

    docker logs -f hub

## Troubleshooting commands

The following commands provide additional detail about installed packages,
versions, and system information that may be helpful when troubleshooting
a JupyterHub deployment. The commands are:

- System and deployment information

```bash
jupyter troubleshoot
```

- Kernel information

```bash
jupyter kernelspec list
```

- Debug logs when running JupyterHub

```bash
jupyterhub --debug
```

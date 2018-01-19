"""
End-to-end Telepresence tests for running directly in the operating system.
"""

import os
from sys import executable
from json import (
    JSONDecodeError,
    loads, dumps,
)
from unittest import (
    TestCase,
)
from subprocess import (
    CalledProcessError,
    PIPE, STDOUT, Popen, check_output, check_call,
)
from pathlib import Path
from shutil import which

from .utils import (
    KUBECTL,
    random_name,
    telepresence_version,
    run_webserver,
)

from .rwlock import RWLock


REGISTRY = os.environ.get("TELEPRESENCE_REGISTRY", "datawire")

network = RWLock()


class ResourceIdent(object):
    def __init__(self, namespace, name):
        self.namespace = namespace
        self.name = name


def _telepresence(telepresence_args):
    """
    Run a probe in a Telepresence execution context.
    """
    args = [
        executable, which("telepresence"),
        "--logfile", "-",
    ] + telepresence_args
    return check_output(
        args=args,
        stdin=PIPE,
        stderr=STDOUT,
    )



class _EndToEndTestsMixin(object):
    """
    A mixin for ``TestCase`` defining various end-to-end tests for
    Telepresence.
    """
    DESIRED_ENVIRONMENT = {
        "MYENV": "hello",
        "EXAMPLE_ENVFROM": "foobar",

        # XXXX
        # Container method doesn't support multi-line environment variables.
        # Therefore disable this for all methods or the container tests all
        # fail...
        # XXXX
        # "EX_MULTI_LINE": (
        #     "first line (no newline before, newline after)\n"
        #     "second line (newline before and after)\n"
        # ),
    }

    def __init__(self, method, operation):
        self._method = method
        self._operation = operation


    def setUp(self):
        probe_endtoend = (Path(__file__).parent / "probe_endtoend.py").as_posix()

        # Create a web server service.  We'll observe side-effects related to
        # this, such as things set in our environment, and also interact with
        # it directly to demonstrate behaviors related to networking.  It's
        # important that we create this before the ``prepare_deployment`` step
        # below because the environment supplied by Kubernetes to a
        # Deployment's containers depends on the state of the cluster at the
        # time of pod creation.
        deployment_ident = ResourceIdent(
            namespace=random_name(),
            name=random_name(),
        )
        create_namespace(deployment_ident.namespace, deployment_ident.name)
        self.webserver_name = run_webserver(deployment_ident.namespace)

        self._operation.prepare_deployment(deployment_ident, self.DESIRED_ENVIRONMENT)
        print("Prepared deployment {}/{}".format(deployment_ident.namespace, deployment_ident.name))
        self.addCleanup(self._cleanup_deployment, deployment_ident)

        operation_args = self._operation.telepresence_args(deployment_ident)
        method_args = self._method.telepresence_args(probe_endtoend)
        args = operation_args + method_args
        try:
            try:
                self._method.lock()
                output = _telepresence(args)
            finally:
                self._method.unlock()
        except CalledProcessError as e:
            self.fail("Failure running {}: {}\n{}".format(
                ["telepresence"] + args, str(e), e.output.decode("utf-8"),
            ))
        else:
            # Scrape the payload out of the overall noise.
            output = output.split(b"{probe delimiter}")[1]
            try:
                self.probe_result = loads(output)
            except JSONDecodeError:
                self.fail("Could not decode JSON probe result from {}:\n{}".format(
                    ["telepresence"] + args, output.decode("utf-8"),
                ))


    def test_environment_from_deployment(self):
        """
        The Telepresence execution context supplies environment variables with
        values defined in the Kubernetes Deployment.
        """
        probe_environment = self.probe_result["environ"]
        self.assertEqual(
            self.DESIRED_ENVIRONMENT,
            {k: probe_environment.get(k, None) for k in self.DESIRED_ENVIRONMENT},
            "Probe environment missing some expected items:\n"
            "Desired: {}\n"
            "Probed: {}\n".format(self.DESIRED_ENVIRONMENT, probe_environment),
        )


    def test_environment_for_services(self):
        """
        The Telepresence execution context supplies environment variables with
        values locating services configured on the cluster.
        """
        probe_environment = self.probe_result["environ"]
        service_env = self.webserver_name.upper().replace("-", "_")
        host = probe_environment[service_env + "_SERVICE_HOST"]
        port = probe_environment[service_env + "_SERVICE_PORT"]

        prefix = service_env + "_PORT_{}_TCP".format(port)
        desired_environment = {
            service_env + "_PORT": "tcp://{}:{}".format(host, port),
            prefix + "_PROTO": "tcp",
            prefix + "_PORT": port,
            prefix + "_ADDR": host,
        }

        self.assertEqual(
            desired_environment,
            {k: probe_environment.get(k, None) for k in desired_environment},
            "Probe environment missing some expected items:\n"
            "Desired: {}\n"
            "Probed: {}\n".format(desired_environment, probe_environment),
        )
        self.assertEqual(
            probe_environment[prefix],
            probe_environment[service_env + "_PORT"],
        )


    def _cleanup_deployment(self, ident):
        check_call([
            KUBECTL, "delete",
            "--namespace", ident.namespace,
            "--ignore-not-found",
            "deployment", ident.name,
        ])



class _VPNTCPMethod(object):
    def lock(self):
        network.lock_write()


    def unlock(self):
        network.unlock_write()


    def telepresence_args(self, probe):
        return [
            "--method", "vpn-tcp",
            "--run", executable, probe,
        ]



class _InjectTCPMethod(object):
    def lock(self):
        network.lock_read()


    def unlock(self):
        network.unlock_read()


    def telepresence_args(self, probe):
        return [
            "--method", "inject-tcp",
            "--run", executable, probe,
        ]



class _ContainerMethod(object):
    def lock(self):
        network.lock_read()


    def unlock(self):
        network.unlock_read()


    def telepresence_args(self, probe):
        return [
            "--method", "container",
            "--docker-run",
            "--volume", "{}:/probe.py".format(probe),
            "python:3-alpine",
            "python", "/probe.py",
        ]


def create_deployment(deployment_ident, image, environ):
    def env_arguments(environ):
        return list(
            "--env={}={}".format(k, v)
            for (k, v)
            in environ.items()
        )
    deployment = dumps({
        "kind": "Deployment",
        "apiVersion": "extensions/v1beta1",
        "metadata": {
            "name": deployment_ident.name,
            "namespace": deployment_ident.namespace,
        },
        "spec": {
            "replicas": 2,
            "template": {
                "metadata": {
                    "labels": {
                        "name": deployment_ident.name,
                        "telepresence-test": deployment_ident.name,
                    },
                },
                "spec": {
                    "containers": [{
                        "name": "hello",
                        "image": image,
                        "env": list(
                            {"name": k, "value": v}
                            for (k, v)
                            in environ.items()
                        ),
                    }],
                },
            },
        },
    })
    check_output([KUBECTL, "create", "-f", "-"], input=deployment.encode("utf-8"))



def create_namespace(namespace_name, name):
    namespace = dumps({
        "kind": "Namespace",
        "apiVersion": "v1",
        "metadata": {
            "name": namespace_name,
            "labels": {
                "telepresence-test": name,
            },
        },
    })
    check_output([KUBECTL, "create", "-f", "-"], input=namespace.encode("utf-8"))



class _ExistingDeploymentOperation(object):
    def __init__(self, swap):
        self.swap = swap


    def prepare_deployment(self, deployment_ident, environ):
        if self.swap:
            image = "openshift/hello-openshift"
        else:
            image = "{}/telepresence-k8s:{}".format(
                REGISTRY,
                telepresence_version(),
            )
        create_deployment(deployment_ident, image, environ)


    def telepresence_args(self, deployment_ident):
        if self.swap:
            option = "--swap-deployment"
        else:
            option = "--deployment"
        return [
            "--namespace", deployment_ident.namespace,
            option, deployment_ident.name,
        ]



class _NewDeploymentOperation(object):
    def prepare_deployment(self, deployment_ident, environ):
        pass

    def telepresence_args(self, deployment_ident):
        return [
            "--namespace", deployment_ident.namespace,
            "--new-deployment", deployment_ident.name,
        ]



def telepresence_tests(method, operation):
    class Tests(_EndToEndTestsMixin, TestCase):
        def __init__(self, *args, **kwargs):
            _EndToEndTestsMixin.__init__(self, method, operation)
            TestCase.__init__(self, *args, **kwargs)
    return Tests



class SwapEndToEndVPNTCPTests(telepresence_tests(
        _VPNTCPMethod(),
        _ExistingDeploymentOperation(True),
)):
    """
    Tests for the *vpn-tcp* method using a swapped Deployment.
    """



class SwapEndToEndInjectTCPTests(telepresence_tests(
        _InjectTCPMethod(),
        _ExistingDeploymentOperation(True),
)):
    """
    Tests for the *inject-tcp* method using a swapped Deployment.
    """



class SwapEndToEndContainerTests(telepresence_tests(
        _ContainerMethod(),
        _ExistingDeploymentOperation(True),
)):
    """
    Tests for the *container* method using a swapped Deployment.
    """


class ExistingEndToEndVPNTCPTests(telepresence_tests(
        _VPNTCPMethod(),
        _ExistingDeploymentOperation(False),
)):
    """
    Tests for the *vpn-tcp* method using an existing Deployment.
    """



class ExistingEndToEndInjectTCPTests(telepresence_tests(
        _InjectTCPMethod(),
        _ExistingDeploymentOperation(False),
)):
    """
    Tests for the *inject-tcp* method using an existing Deployment.
    """



class ExistingEndToEndContainerTests(telepresence_tests(
        _ContainerMethod(),
        _ExistingDeploymentOperation(False),
)):
    """
    Tests for the *container* method using an existing Deployment.
    """


class NewEndToEndVPNTCPTests(telepresence_tests(
        _VPNTCPMethod(),
        _NewDeploymentOperation(),
)):
    """
    Tests for the *vpn-tcp* method creating a new Deployment.
    """
    test_environment_from_deployment = None


class NewEndToEndInjectTCPTests(telepresence_tests(
        _InjectTCPMethod(),
        _NewDeploymentOperation(),
)):
    """
    Tests for the *inject-tcp* method creating a new Deployment.
    """
    test_environment_from_deployment = None



class NewEndToEndContainerTests(telepresence_tests(
        _ContainerMethod(),
        _NewDeploymentOperation(),
)):
    """
    Tests for the *container* method creating a new Deployment.
    """
    test_environment_from_deployment = None

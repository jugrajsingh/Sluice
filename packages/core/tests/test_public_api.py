import sluice_core as sc


def test_public_exports():
    for name in (
        "Queue",
        "ObjectStore",
        "AppRegistry",
        "Cache",
        "ClusterInspector",
        "ComputeProvider",
        "InferenceHandler",
        "InferenceObjects",
        "Message",
        "QueueDepth",
        "AppSpec",
        "AppStatus",
        "KubernetesCandidate",
        "VmCandidate",
        "K8sPlacementSpec",
        "VmPlacementSpec",
        "Toleration",
        "VmRecord",
        "VmState",
        "WorkerStatus",
        "WorkerState",
        "Settings",
        "parse_app_yaml",
        "serialize_app_yaml",
    ):
        assert hasattr(sc, name), name

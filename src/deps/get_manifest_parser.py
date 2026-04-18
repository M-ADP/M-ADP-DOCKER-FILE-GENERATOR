from src.core.manifest.parser import ManifestParser
from src.core.manifest.parsers.go import GoManifestParser
from src.core.manifest.parsers.java_gradle import JavaGradleManifestParser
from src.core.manifest.parsers.java_maven import JavaMavenManifestParser
from src.core.manifest.parsers.node import NodeManifestParser
from src.core.manifest.parsers.php import PhpManifestParser
from src.core.manifest.parsers.python import PythonManifestParser
from src.core.manifest.parsers.ruby import RubyManifestParser
from src.core.manifest.parsers.rust import RustManifestParser
from src.core.manifest.parsers.unknown import UnknownManifestParser
from src.core.manifest.port_detector import PortDetector


def get_manifest_parser() -> ManifestParser:
    port_detector = PortDetector()
    return ManifestParser(
        parsers=[
            NodeManifestParser(port_detector),
            PythonManifestParser(port_detector),
            JavaGradleManifestParser(port_detector),
            JavaMavenManifestParser(port_detector),
            GoManifestParser(port_detector),
            RustManifestParser(port_detector),
            RubyManifestParser(port_detector),
            PhpManifestParser(port_detector),
        ],
        fallback=UnknownManifestParser(port_detector),
    )

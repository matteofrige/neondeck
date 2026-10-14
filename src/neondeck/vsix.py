"""Build the VS Code bridge extension (.vsix) from the packaged sources."""
from __future__ import annotations

import json
import zipfile
from importlib import resources
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
  <Default Extension=".xml" ContentType="text/xml"/>
  <Default Extension=".md" ContentType="text/markdown"/>
</Types>
"""


def bridge_dir():
    return resources.files("neondeck") / "vscode_bridge"


def bridge_meta() -> dict:
    return json.loads((bridge_dir() / "package.json").read_text(encoding="utf-8"))


def extension_id() -> str:
    meta = bridge_meta()
    return f"{meta['publisher']}.{meta['name']}"


def manifest_xml(meta: dict) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id={quoteattr(meta['name'])} Version={quoteattr(meta['version'])} Publisher={quoteattr(meta['publisher'])}/>
    <DisplayName>{escape(meta.get('displayName', meta['name']))}</DisplayName>
    <Description xml:space="preserve">{escape(meta.get('description', ''))}</Description>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
  <Properties>
    <Property Id="Microsoft.VisualStudio.Code.Engine" Value={quoteattr(meta['engines']['vscode'])}/>
  </Properties>
</PackageManifest>
"""


def build_vsix(dest: Path) -> Path:
    """Write the .vsix to `dest` (a file path, or a directory for the default name)."""
    meta = bridge_meta()
    dest = Path(dest).expanduser()
    if dest.is_dir():
        dest = dest / f"{meta['publisher']}.{meta['name']}-{meta['version']}.vsix"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension.vsixmanifest", manifest_xml(meta))
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        for entry in sorted(bridge_dir().iterdir(), key=lambda e: e.name):
            if entry.is_file():
                z.writestr(f"extension/{entry.name}", entry.read_bytes())
    return dest

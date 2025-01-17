import io
import pathlib
import re
from collections import defaultdict
from copy import deepcopy
from os import PathLike
from typing import Any

import glyphsLib
import openstep_plist
from fontra.core.classes import (
    Anchor,
    Axes,
    Component,
    DiscreteFontAxis,
    FontAxis,
    FontInfo,
    FontSource,
    GlyphAxis,
    GlyphSource,
    Guideline,
    ImageData,
    Kerning,
    Layer,
    LineMetric,
    OpenTypeFeatures,
    StaticGlyph,
    VariableGlyph,
)
from fontra.core.path import PackedPathPointPen
from fontra.core.protocols import WritableFontBackend
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.misc.transform import DecomposedTransform
from glyphsLib.builder.axes import (
    get_axis_definitions,
    get_regular_master,
    to_designspace_axes,
)
from glyphsLib.builder.smart_components import Pole

rootInfoNames = [
    "familyName",
    "versionMajor",
    "versionMinor",
]


infoNamesMapping = [
    # (Fontra, Glyphs)
    ("copyright", "copyrights"),
    ("designer", "designers"),
    ("designerURL", "designerURL"),
    ("licenseDescription", "licenses"),
    # ("licenseInfoURL", "licensesURL"),  # Not defined in glyphsLib
    ("manufacturer", "manufacturers"),
    ("manufacturerURL", "manufacturerURL"),
    ("trademark", "trademarks"),
    ("vendorID", "vendorID"),
]

GS_KERN_GROUP_PREFIXES = {
    side: f"@MMK_{side[0].upper()}_" for side in ["left", "right", "top", "bottom"]
}
FONTRA_KERN_GROUP_PREFIXES = {
    "left": "public.kern1.",
    "right": "public.kern2.",
    "top": "kern.top.",
    "bottom": "kern.bottom.",
}
GS_FORMAT_2_KERN_SIDES = [
    # pair side, glyph side
    ("left", "rightKerningGroup"),
    ("right", "leftKerningGroup"),
    ("top", "bottomKerningGroup"),
    ("bottom", "topKerningGroup"),
]
GS_FORMAT_3_KERN_SIDES = [
    # pair side, glyph side
    ("left", "kernRight"),
    ("right", "kernLeft"),
    ("top", "kernBottom"),
    ("bottom", "kernTop"),
]


class GlyphsBackend:
    @classmethod
    def fromPath(cls, path: PathLike) -> WritableFontBackend:
        self = cls()
        self._setupFromPath(path)
        return self

    def _setupFromPath(self, path: PathLike) -> None:
        gsFont = glyphsLib.classes.GSFont()
        self.gsFilePath = path

        rawFontData, rawGlyphsData = self._loadFiles(path)

        parser = glyphsLib.parser.Parser(current_type=gsFont.__class__)
        parser.parse_into_object(gsFont, rawFontData)

        self.gsFont = gsFont

        # Fill the glyphs list with dummy placeholder glyphs
        self.gsFont.glyphs = [
            glyphsLib.classes.GSGlyph() for i in range(len(rawGlyphsData))
        ]
        self.rawFontData = rawFontData
        self.rawGlyphsData = rawGlyphsData

        self.glyphNameToIndex = {
            glyphData["glyphname"]: i for i, glyphData in enumerate(rawGlyphsData)
        }
        self.parsedGlyphNames: set[str] = set()

        dsAxes = [
            dsAxis
            for dsAxis in gsAxesToDesignSpaceAxes(self.gsFont)
            # Ignore axes without any range
            if dsAxis.minimum != dsAxis.maximum
        ]

        self.axisNames = {axis.name for axis in dsAxes}

        self.locationByMasterID = {}
        for master in self.gsFont.masters:
            location = {}
            for axisDef in get_axis_definitions(self.gsFont):
                if axisDef.name in self.axisNames:
                    location[axisDef.name] = axisDef.get_design_loc(master)
            self.locationByMasterID[master.id] = location

        self.glyphMap, self.kerningGroups = _readGlyphMapAndKerningGroups(
            rawGlyphsData,
            self.gsFont.format_version,
        )

        axes: list[FontAxis | DiscreteFontAxis] = []
        for dsAxis in dsAxes:
            axis = FontAxis(
                minValue=dsAxis.minimum,
                defaultValue=dsAxis.default,
                maxValue=dsAxis.maximum,
                label=dsAxis.name,
                name=dsAxis.name,
                tag=dsAxis.tag,
                hidden=dsAxis.hidden,
            )
            if dsAxis.map:
                axis.mapping = [[a, b] for a, b in dsAxis.map]
            axes.append(axis)
        self.axes = axes

    @staticmethod
    def _loadFiles(path: PathLike) -> tuple[dict[str, Any], list[Any]]:
        with open(path, "r", encoding="utf-8") as fp:
            rawFontData = openstep_plist.load(fp, use_numbers=True)

        rawGlyphsData = rawFontData["glyphs"]
        rawFontData["glyphs"] = []
        return rawFontData, rawGlyphsData

    async def getGlyphMap(self) -> dict[str, list[int]]:
        return self.glyphMap

    async def putGlyphMap(self, value: dict[str, list[int]]) -> None:
        print("GlyphsBackend putGlyphMap: ", value)
        pass

    async def deleteGlyph(self, glyphName):
        print("GlyphsBackend deleteGlyph: ", glyphName)
        pass

    async def getFontInfo(self) -> FontInfo:
        infoDict = {}
        for name in rootInfoNames:
            value = getattr(self.gsFont, name, None)
            if value is not None:
                infoDict[name] = value

        properties = {p.key: p.value for p in self.gsFont.properties}
        for fontraName, glyphsName in infoNamesMapping:
            value = properties.get(glyphsName)
            if value is not None:
                infoDict[fontraName] = value

        return FontInfo(**infoDict)

    async def putFontInfo(self, fontInfo: FontInfo):
        print("GlyphsBackend putFontInfo: ", fontInfo)
        pass

    async def getSources(self) -> dict[str, FontSource]:
        return gsMastersToFontraFontSources(self.gsFont, self.locationByMasterID)

    async def putSources(self, sources: dict[str, FontSource]) -> None:
        print("GlyphsBackend putSources: ", sources)
        pass

    async def getAxes(self) -> Axes:
        return Axes(axes=self.axes)

    async def putAxes(self, axes: Axes) -> None:
        print("GlyphsBackend putAxes: ", axes)
        pass

    async def getUnitsPerEm(self) -> int:
        return self.gsFont.upm

    async def putUnitsPerEm(self, value: int) -> None:
        print("GlyphsBackend putUnitsPerEm: ", value)
        pass

    async def getKerning(self) -> dict[str, Kerning]:
        # TODO: RTL kerning: https://docu.glyphsapp.com/#GSFont.kerningRTL
        kerningLTR = gsKerningToFontraKerning(
            self.gsFont, self.kerningGroups, "kerning", "left", "right"
        )
        kerningAttr = (
            "vertKerning" if self.gsFont.format_version == 2 else "kerningVertical"
        )
        kerningVertical = gsKerningToFontraKerning(
            self.gsFont, self.kerningGroups, kerningAttr, "top", "bottom"
        )

        kerning = {}
        if kerningLTR.values:
            kerning["kern"] = kerningLTR
        if kerningVertical.values:
            kerning["vkrn"] = kerningVertical
        return kerning

    async def putKerning(self, kerning: dict[str, Kerning]) -> None:
        print("GlyphsBackend putKerning: ", kerning)
        pass

    async def getFeatures(self) -> OpenTypeFeatures:
        # TODO: extract features
        return OpenTypeFeatures()

    async def putFeatures(self, features: OpenTypeFeatures) -> None:
        print("GlyphsBackend putFeatures: ", features)
        pass

    async def getBackgroundImage(self, imageIdentifier: str) -> ImageData | None:
        return None

    async def putBackgroundImage(self, imageIdentifier: str, data: ImageData) -> None:
        print("GlyphsBackend putBackgroundImage: ", imageIdentifier, data)
        pass

    async def getCustomData(self) -> dict[str, Any]:
        return {}

    async def putCustomData(self, lib):
        print("GlyphsBackend putCustomData: ", lib)
        pass

    async def getGlyph(self, glyphName: str) -> VariableGlyph | None:
        if glyphName not in self.glyphNameToIndex:
            return None

        self._ensureGlyphIsParsed(glyphName)

        gsGlyph = self.gsFont.glyphs[glyphName]

        customData = {}
        if gsGlyph.color is not None:
            customData["com.glyphsapp.glyph-color"] = gsGlyph.color

        localAxes = gsLocalAxesToFontraLocalAxes(gsGlyph)
        localAxesByName = {axis.name: axis for axis in localAxes}
        sources = []
        layers = {}

        seenMasterIDs: dict[str, None] = {}
        gsLayers = []
        for i, gsLayer in enumerate(gsGlyph.layers):
            gsLayers.append((i, gsLayer))
            assert gsLayer.associatedMasterId
            # We use a dict as a set, because we need the insertion order
            seenMasterIDs[gsLayer.associatedMasterId] = None

        masterOrder = {masterID: i for i, masterID in enumerate(seenMasterIDs)}
        gsLayers = sorted(
            gsLayers, key=lambda i_gsLayer: masterOrder[i_gsLayer[1].associatedMasterId]
        )

        seenLocations = []
        for i, gsLayer in gsLayers:
            braceLocation = self._getBraceLayerLocation(gsLayer)
            smartLocation = self._getSmartLocation(gsLayer, localAxesByName)
            masterName = self.gsFont.masters[gsLayer.associatedMasterId].name
            if braceLocation or smartLocation:
                sourceName = f"{masterName} / {gsLayer.name}"
            else:
                sourceName = gsLayer.name or masterName
            layerName = f"{sourceName} (layer #{i})"

            location = {
                **self.locationByMasterID[gsLayer.associatedMasterId],
                **braceLocation,
                **smartLocation,
            }

            if location in seenLocations:
                inactive = True
            else:
                seenLocations.append(location)
                inactive = False

            sources.append(
                GlyphSource(
                    name=sourceName,
                    location=location,
                    layerName=layerName,
                    inactive=inactive,
                )
            )
            layers[layerName] = gsLayerToFontraLayer(gsLayer, self.axisNames)

        fixSourceLocations(sources, set(smartLocation))

        glyph = VariableGlyph(
            name=glyphName,
            axes=localAxes,
            sources=sources,
            layers=layers,
            customData=customData,
        )
        return glyph

    def _ensureGlyphIsParsed(self, glyphName: str) -> None:
        if glyphName in self.parsedGlyphNames:
            return

        glyphIndex = self.glyphNameToIndex[glyphName]
        rawGlyphData = self.rawGlyphsData[glyphIndex]
        # self.rawGlyphsData[glyphIndex] = None
        self.parsedGlyphNames.add(glyphName)

        gsGlyph = glyphsLib.classes.GSGlyph()
        p = glyphsLib.parser.Parser(
            current_type=gsGlyph.__class__, format_version=self.gsFont.format_version
        )
        p.parse_into_object(gsGlyph, rawGlyphData)
        self.gsFont.glyphs[glyphIndex] = gsGlyph

        # Load all component dependencies
        componentNames = set()
        for layer in gsGlyph.layers:
            for component in layer.components:
                componentNames.add(component.name)

        for compoName in sorted(componentNames):
            self._ensureGlyphIsParsed(compoName)

    def _getBraceLayerLocation(self, gsLayer):
        if not gsLayer._is_brace_layer():
            return {}

        return dict(
            (axis.name, value)
            for axis, value in zip(self.axes, gsLayer._brace_coordinates())
        )

    def _getSmartLocation(self, gsLayer, localAxesByName):
        location = {
            name: (
                localAxesByName[name].minValue
                if poleValue == Pole.MIN
                else localAxesByName[name].maxValue
            )
            for name, poleValue in gsLayer.smartComponentPoleMapping.items()
        }
        return {
            disambiguateLocalAxisName(name, self.axisNames): value
            for name, value in location.items()
            if value != localAxesByName[name].defaultValue
        }

    async def putGlyph(
        self, glyphName: str, glyph: VariableGlyph, codePoints: list[int]
    ) -> None:
        # 1. convert VariableGlyph to GSGlyph (but start with a copy of the original)
        gsGlyphNew = variableGlyphToGsGlyph(
            glyph, deepcopy(self.gsFont.glyphs[glyphName])
        )

        # 2. serialize to text with glyphsLib.writer.Writer(), using io.StringIO or io.BytesIO
        f = io.StringIO()
        writer = glyphsLib.writer.Writer(f)
        writer.format_version = self.gsFont.format_version
        writer.write(gsGlyphNew)

        # # 3. parse stream into "raw" object
        # f.seek(0)
        # rawGlyphData = openstep_plist.load(f, use_numbers=True)

        # self._writeRawGlyph(glyphName, rawGlyphData)

        self._findAndReplaceGlyph(glyphName, f)

    def _findAndReplaceGlyph(self, glyphName, f):
        glyphChunkIndicator = f"glyphname = {glyphName};"

        # find glyph chunk
        glyphChunkStart = None
        glyphChunkEnd = None

        with open(self.gsFilePath, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
            for i, line in enumerate(lines):
                if glyphChunkIndicator in line:
                    for j in range(i, 0, -1):
                        if "{" in lines[j]:
                            glyphChunkStart = j
                            break

                    braceLeftCount = 1
                    for k in range(i, len(lines)):
                        if "{" in lines[k]:
                            braceLeftCount += 1
                        if "}" in lines[k]:
                            braceLeftCount -= 1
                        if "}" in lines[k] and braceLeftCount == 0:
                            glyphChunkEnd = k
                            break
                    break

            if glyphChunkStart is None or glyphChunkEnd is None:
                # If not found, maybe add as a new glyph at the end of the glyphs list.
                print("ERROR: Could not find glyph: ", glyphChunkIndicator)
                return

            rawGlyphAsText = f.getvalue()
            newLines = (
                lines[:glyphChunkStart] + [rawGlyphAsText[:-2]] + lines[glyphChunkEnd:]
            )
            with open(self.gsFilePath, "w", encoding="utf-8") as fp:
                fp.writelines(newLines)

    def _writeRawGlyph(self, glyphName, rawGlyphData):
        # 4. replace original "raw" object with new "raw" object
        glyphIndex = self.glyphNameToIndex[glyphName]
        self.rawGlyphsData[glyphIndex] = rawGlyphData
        self.rawFontData["glyphs"] = self.rawGlyphsData

        # 5. write whole file with openstep_plist
        with open(self.gsFilePath, "w", encoding="utf-8") as fp:
            openstep_plist.dump(
                self.rawFontData,
                fp,
                unicode_escape=False,
                indent=0,
                single_line_tuples=True,
                escape_newlines=False,
            )

        # 6. fix formatting
        saveFileWithGsFormatting(self.gsFilePath)

    async def aclose(self) -> None:
        pass


class GlyphsPackageBackend(GlyphsBackend):
    @staticmethod
    def _loadFiles(path: PathLike) -> tuple[dict[str, Any], list[Any]]:
        packagePath = pathlib.Path(path)
        fontInfoPath = packagePath / "fontinfo.plist"
        orderPath = packagePath / "order.plist"
        glyphsPath = packagePath / "glyphs"

        glyphOrder = []
        if orderPath.exists():
            with open(orderPath, "r", encoding="utf-8") as fp:
                glyphOrder = openstep_plist.load(fp)
        glyphNameToIndex = {glyphName: i for i, glyphName in enumerate(glyphOrder)}

        with open(fontInfoPath, "r", encoding="utf-8") as fp:
            rawFontData = openstep_plist.load(fp, use_numbers=True)

        rawFontData["glyphs"] = []

        rawGlyphsData = []
        for glyphfile in glyphsPath.glob("*.glyph"):
            with open(glyphfile, "r") as fp:
                glyphData = openstep_plist.load(fp, use_numbers=True)
            rawGlyphsData.append(glyphData)

        def sortKey(glyphData):
            glyphName = glyphData["glyphname"]
            index = glyphNameToIndex.get(glyphName)
            if index is not None:
                return (0, index)
            else:
                return (1, glyphName)

        rawGlyphsData.sort(key=sortKey)

        return rawFontData, rawGlyphsData

    def _writeRawGlyph(self, glyphName, rawGlyphData):
        # 5. write glyh specific file with openstep_plist
        filePath = self.getGlyphFilePath(glyphName)
        with open(filePath, "w", encoding="utf-8") as fp:
            openstep_plist.dump(
                rawGlyphData,
                fp,
                unicode_escape=False,
                indent=0,
                single_line_tuples=True,
                escape_newlines=False,
            )

        # 6. fix formatting
        saveFileWithGsFormatting(filePath)

    def _findAndReplaceGlyph(self, glyphName, f):
        filePath = self.getGlyphFilePath(glyphName)
        filePath.write_text(f.getvalue(), encoding="utf=8")

    def getGlyphFilePath(self, glyphName):
        glyphsPath = pathlib.Path(self.gsFilePath) / "glyphs"
        # TODO: Get the right glyph file name might be challenging,
        # because for example the glyph A-cy is stored in the package as A_-cy.glyph
        realGlyphName = getGlyphspackageGlyphFileName(glyphName)
        return glyphsPath / (realGlyphName + ".glyph")


def getGlyphspackageGlyphFileName(glyphName):
    nameParts = glyphName.split("-")
    firstPart = (
        f"{nameParts[0]}_"
        if len(nameParts[0]) == 1 and nameParts[0].isupper()
        else nameParts[0]
    )
    nameParts[0] = firstPart

    return "-".join(nameParts)


def _readGlyphMapAndKerningGroups(
    rawGlyphsData: list, formatVersion: int
) -> tuple[dict[str, list[int]], dict[str, tuple[str, str]]]:
    glyphMap = {}
    kerningGroups: dict = defaultdict(lambda: defaultdict(list))

    sideAttrs = GS_FORMAT_2_KERN_SIDES if formatVersion == 2 else GS_FORMAT_3_KERN_SIDES

    for glyphData in rawGlyphsData:
        glyphName = glyphData["glyphname"]

        # extract code points
        codePoints = glyphData.get("unicode")
        if codePoints is None:
            codePoints = []
        elif formatVersion == 2:
            if isinstance(codePoints, str):
                codePoints = [int(codePoint, 16) for codePoint in codePoints.split(",")]
            else:
                assert isinstance(codePoints, int)
                # The plist parser turned it into an int, but it was a hex string
                codePoints = [int(str(codePoints), 16)]
        elif isinstance(codePoints, int):
            codePoints = [codePoints]
        else:
            assert all(isinstance(codePoint, int) for codePoint in codePoints)
        glyphMap[glyphName] = codePoints

        # extract kern groups
        for pairSide, glyphSideAttr in sideAttrs:
            groupName = glyphData.get(glyphSideAttr)
            if groupName is not None:
                kerningGroups[pairSide][
                    FONTRA_KERN_GROUP_PREFIXES[pairSide] + groupName
                ].append(glyphName)

    return glyphMap, kerningGroups


def gsLayerToFontraLayer(gsLayer, globalAxisNames):
    pen = PackedPathPointPen()
    gsLayer.drawPoints(pen)

    components = [
        gsComponentToFontraComponent(gsComponent, gsLayer, globalAxisNames)
        for gsComponent in gsLayer.components
    ]

    anchors = [gsAnchorToFontraAnchor(gsAnchor) for gsAnchor in gsLayer.anchors]

    return Layer(
        glyph=StaticGlyph(
            xAdvance=gsLayer.width,
            path=pen.getPath(),
            components=components,
            anchors=anchors,
        )
    )


def gsComponentToFontraComponent(gsComponent, gsLayer, globalAxisNames):
    component = Component(
        name=gsComponent.name,
        transformation=DecomposedTransform.fromTransform(gsComponent.transform),
        location={
            disambiguateLocalAxisName(name, globalAxisNames): value
            for name, value in gsComponent.smartComponentValues.items()
        },
    )
    return component


def disambiguateLocalAxisName(axisName, globalAxisNames):
    return f"{axisName} (local)" if axisName in globalAxisNames else axisName


def gsAnchorToFontraAnchor(gsAnchor):
    anchor = Anchor(
        name=gsAnchor.name,
        x=gsAnchor.position.x,
        y=gsAnchor.position.y,
        # TODO: gsAnchor.orientation – If the position of the anchor
        # is relative to the LSB (0), center (2) or RSB (1).
        # Details: https://docu.glyphsapp.com/#GSAnchor.orientation
        customData=gsAnchor.userData if gsAnchor.userData else dict(),
    )
    return anchor


def gsGuidelineToFontraGuideline(gsGuideline):
    return Guideline(
        x=gsGuideline.position.x,
        y=gsGuideline.position.y,
        angle=gsGuideline.angle,
        name=gsGuideline.name,
        locked=gsGuideline.locked,
    )


class MinimalUFOBuilder:
    def __init__(self, gsFont):
        self.font = gsFont
        self.designspace = DesignSpaceDocument()
        self.minimize_glyphs_diffs = False

    to_designspace_axes = to_designspace_axes


def gsAxesToDesignSpaceAxes(gsFont):
    builder = MinimalUFOBuilder(gsFont)
    builder.to_designspace_axes()
    return builder.designspace.axes


def gsLocalAxesToFontraLocalAxes(gsGlyph):
    basePoleMapping = gsGlyph.layers[0].smartComponentPoleMapping
    return [
        GlyphAxis(
            name=axis.name,
            minValue=axis.bottomValue,
            defaultValue=(
                axis.bottomValue
                if basePoleMapping[axis.name] == Pole.MIN
                else axis.topValue
            ),
            maxValue=axis.topValue,
        )
        for axis in gsGlyph.smartComponentAxes
    ]


def fixSourceLocations(sources, smartAxisNames):
    # If a set of sources is equally controlled by a font axis and a glyph axis
    # (smart axis), then the font axis should be ignored. This makes our
    # varLib-based variation model behave like Glyphs.
    sets = defaultdict(set)
    for i, source in enumerate(sources):
        for locItem in source.location.items():
            sets[locItem].add(i)

    reverseSets = defaultdict(set)
    for locItem, sourceIndices in sets.items():
        reverseSets[tuple(sorted(sourceIndices))].add(locItem)

    matches = [locItems for locItems in reverseSets.values() if len(locItems) > 1]

    locItemsToDelete = []
    for locItems in matches:
        for axis, value in locItems:
            if axis not in smartAxisNames:
                locItemsToDelete.append((axis, value))

    for axis, value in locItemsToDelete:
        for source in sources:
            if source.location.get(axis) == value:
                del source.location[axis]


def translateGroupName(name, oldPrefix, newPrefix):
    return newPrefix + name[len(oldPrefix) :] if name.startswith(oldPrefix) else name


def gsKerningToFontraKerning(
    gsFont, groupsBySide, kerningAttr, side1, side2
) -> Kerning:
    gsPrefix1 = GS_KERN_GROUP_PREFIXES[side1]
    gsPrefix2 = GS_KERN_GROUP_PREFIXES[side2]
    fontraPrefix1 = FONTRA_KERN_GROUP_PREFIXES[side1]
    fontraPrefix2 = FONTRA_KERN_GROUP_PREFIXES[side2]

    groups = dict(groupsBySide[side1] | groupsBySide[side2])

    sourceIdentifiers = []
    valueDicts: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))

    defaultMasterID = get_regular_master(gsFont).id

    for gsMaster in gsFont.masters:
        kernDict = getattr(gsFont, kerningAttr).get(gsMaster.id, {})
        if not kernDict and gsMaster.id != defaultMasterID:
            # Even if the default master does not contain kerning, it makes life
            # easier down the road if we include this empty kerning, lest we run
            # into "missing base master"-type interpolation errors.
            continue

        sourceIdentifiers.append(gsMaster.id)

        for name1, name2Dict in kernDict.items():
            name1 = translateGroupName(name1, gsPrefix1, fontraPrefix1)

            for name2, value in name2Dict.items():
                name2 = translateGroupName(name2, gsPrefix2, fontraPrefix2)
                valueDicts[name1][name2][gsMaster.id] = value

    values = {
        left: {
            right: [valueDict.get(key) for key in sourceIdentifiers]
            for right, valueDict in rightDict.items()
        }
        for left, rightDict in valueDicts.items()
    }

    return Kerning(groups=groups, sourceIdentifiers=sourceIdentifiers, values=values)


def gsMastersToFontraFontSources(gsFont, locationByMasterID):
    sources = {}
    for gsMaster in gsFont.masters:
        sources[gsMaster.id] = FontSource(
            name=gsMaster.name,
            italicAngle=gsMaster.italicAngle,
            location=locationByMasterID[gsMaster.id],
            lineMetricsHorizontalLayout=gsVerticalMetricsToFontraLineMetricsHorizontal(
                gsFont, gsMaster
            ),
            guidelines=[
                gsGuidelineToFontraGuideline(gsGuideline)
                for gsGuideline in gsMaster.guides
            ],
        )
    return sources


def gsToFontraZone(gsVerticalMetricsValue, gsAlignmentZones):
    for gsZone in gsAlignmentZones:
        if gsZone.position == gsVerticalMetricsValue:
            return gsZone.size
    return 0


def gsVerticalMetricsToFontraLineMetricsHorizontal(gsFont, gsMaster):
    lineMetricsHorizontal = {
        "ascender": LineMetric(
            value=gsMaster.ascender,
            zone=gsToFontraZone(gsMaster.ascender, gsMaster.alignmentZones),
        ),
        "capHeight": LineMetric(
            value=gsMaster.capHeight,
            zone=gsToFontraZone(gsMaster.capHeight, gsMaster.alignmentZones),
        ),
        "xHeight": LineMetric(
            value=gsMaster.xHeight,
            zone=gsToFontraZone(gsMaster.xHeight, gsMaster.alignmentZones),
        ),
        "baseline": LineMetric(
            value=0, zone=gsToFontraZone(0, gsMaster.alignmentZones)
        ),
        "descender": LineMetric(
            value=gsMaster.descender,
            zone=gsToFontraZone(gsMaster.descender, gsMaster.alignmentZones),
        ),
    }

    # TODO: custom metrics https://docu.glyphsapp.com/#GSFontMaster.metrics
    # Custom vertical metrics seem not to work with GlyphsLib, currently.
    # The following code works within GlyphsApp, but not with GlyphsLib.
    # for gsMetric in gsFont.metrics:
    #     if gsMetric.name:
    #         # if it has a name, it is a custom vertical metric
    #         gsMetricValue = gsMaster.metricValues[gsMetric.id]
    #         print('position: ', gsMetricValue.position)
    #         print('overshoot: ', gsMetricValue.overshoot)
    #         lineMetricsHorizontal[gsMetric.name] = LineMetric(
    #             value=gsMetricValue.position,
    #             zone=gsToFontraZone(gsMetricValue.overshoot, gsMaster.alignmentZones)
    #         )

    return lineMetricsHorizontal


# The following should be obsolte with _findAndReplaceGlyph
def saveFileWithGsFormatting(gsFilePath):
    # openstep_plist.dump changes the whole formatting, therefore
    # it's very diffucute to see what has changed.
    # This function is a very bad try to get close to how the formatting
    # looks like for a .glyphs file.
    # There must be a better solution, but this is better than nothing.
    with open(gsFilePath, "r", encoding="utf-8") as file:
        content = file.read()

    content = re.sub(r"pos = \(\s*(-?\d+),\s*(-?\d+)\s*\);", r"pos = (\1,\2);", content)

    content = re.sub(
        r"pos = \(\s*([\d.]+),\s*([\d.]+)\s*\);", r"pos = (\1,\2);", content
    )

    content = re.sub(
        r"\(\s*([\d.]+),\s*(-?\d+),\s*([a-zA-Z])\s*\)", r"(\1,\2,\3)", content
    )

    content = re.sub(
        r"origin = \(\s*(-?\d+),\s*(-?\d+)\s*\);", r"origin = (\1,\2);", content
    )

    content = re.sub(
        r"target = \(\s*(-?\d+),\s*(-?\d+)\s*\);", r"target = (\1,\2);", content
    )

    content = re.sub(
        r"color = \(\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\s*\);",
        r"color = (\1,\2,\3,\4);",
        content,
    )

    content = re.sub(
        r"\(\s*(-?\d+),\s*(-?\d+),\s*([a-zA-Z]+)\s*\)", r"(\1,\2,\3)", content
    )

    content = re.sub(
        r"\(\s*(-?\d+),\s*(-?\d+),\s*([a-zA-Z]),\s*\{", r"(\1,\2,\3,{", content
    )

    content = re.sub(
        r"\(\s*(-?\d+),\s*(-?\d+),\s*([a-zA-Z]+),\s*\{", r"(\1,\2,\3,{", content
    )

    content = re.sub(r"\}\s*\),", r"}),", content)

    content += "\n"  # add blank break at the end of the file.

    with open(gsFilePath, "w", encoding="utf-8") as file:
        file.write(content)


def variableGlyphToGsGlyph(variableGlyph, gsGlyph):
    # TODO: convert fontra variableGlyph to GlyphsApp glyph
    gsGlyph.name = f"{variableGlyph.name}.changed"

    return gsGlyph

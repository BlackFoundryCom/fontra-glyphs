import pathlib
from collections import defaultdict
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
    Kerning,
    Layer,
    LineMetric,
    OpenTypeFeatures,
    StaticGlyph,
    VariableGlyph,
)
from fontra.core.path import PackedPathPointPen
from fontra.core.protocols import ReadableFontBackend
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.misc.transform import DecomposedTransform
from glyphsLib.builder.axes import get_axis_definitions, to_designspace_axes
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
    "top": "public.kern1.",
    "bottom": "public.kern2.",
}
GS_FORMAT_2_KERN_SIDES = [
    # pair side, glyph side
    ("left", "rightKerningGroup"),
    ("right", "leftKerningGroup"),
    ("top", "bottomKerningGroup"),  # FIXME: unsure if this exists in Glyphs-2 format
    ("bottom", "topKerningGroup"),  # FIXME: same
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
    def fromPath(cls, path: PathLike) -> ReadableFontBackend:
        self = cls()
        self._setupFromPath(path)
        return self

    def _setupFromPath(self, path: PathLike) -> None:
        gsFont = glyphsLib.classes.GSFont()

        rawFontData, rawGlyphsData = self._loadFiles(path)

        parser = glyphsLib.parser.Parser(current_type=gsFont.__class__)
        parser.parse_into_object(gsFont, rawFontData)

        self.gsFont = gsFont

        # Fill the glyphs list with dummy placeholder glyphs
        self.gsFont.glyphs = [
            glyphsLib.classes.GSGlyph() for i in range(len(rawGlyphsData))
        ]
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

    async def getSources(self) -> dict[str, FontSource]:
        return gsMastersToFontraFontSources(self.gsFont, self.locationByMasterID)

    async def getAxes(self) -> Axes:
        return Axes(axes=self.axes)

    async def getUnitsPerEm(self) -> int:
        return self.gsFont.upm

    async def getKerning(self) -> dict[str, Kerning]:
        # TODO: RTL kerning: https://docu.glyphsapp.com/#GSFont.kerningRTL
        kerningLTR = gsKerningToFontraKerning(
            self.gsFont, self.kerningGroups, "kerning", "left", "right"
        )
        kerningVertical = gsKerningToFontraKerning(
            self.gsFont, self.kerningGroups, "kerningVertical", "top", "bottom"
        )

        kerning = {}
        if kerningLTR.values:
            kerning["kern"] = kerningLTR
        if kerningVertical.values:
            kerning["vkrn"] = kerningVertical
        return kerning

    async def getFeatures(self) -> OpenTypeFeatures:
        # TODO: extract features
        return OpenTypeFeatures()

    async def getCustomData(self) -> dict[str, Any]:
        return {}

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
        self.rawGlyphsData[glyphIndex] = None
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

    for gsMaster in gsFont.masters:
        kernDict = getattr(gsFont, kerningAttr).get(gsMaster.id)
        if kernDict is None:
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

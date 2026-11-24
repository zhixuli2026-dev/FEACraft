import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { TrackballControls } from "three/examples/jsm/controls/TrackballControls.js";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { SSAOPass } from "three/examples/jsm/postprocessing/SSAOPass.js";
import { api } from "../api";
import { captureProjectCase, useStore } from "../store";
import { cadAssemblyPayload, cadFitDistance, cadOpacityFor, decodeCadSceneBase64, normalizeCadAssembly, normalizeCadScene, normalizeCadTheme } from "../utils/cadScene";
import type { CadSceneDocument, CadTheme } from "../utils/cadScene";
import { followCadScenes } from "../utils/cadSceneFeed";
import { CAD_SURFACES, CAD_SURFACE_FIELDS, CAD_VIEW_DIRECTIONS, CAD_VIEW_PRESETS, DEFAULT_CAD_GRID,
  cadOrthoSpan, cadQuadViewports, normalizeCadControl, normalizeCadMaterial, normalizeCadSurface, normalizeCadViewMode } from "../utils/cadView";
import type { CadControl, CadGridPlane, CadMaterialSettings, CadSurfaceSettings, CadViewMode, CadViewPreset } from "../utils/cadView";
import { CadQuadView, makeCadControls } from "../utils/cadQuadView";
import { applyCadStudioLighting, cadStudioGradient, DEFAULT_CAD_STUDIO, loadCadEnvironment,
  proceduralCadEnvironment } from "../utils/cadStudioLighting";
import type { CadStudioEnvironment, CadStudioLighting } from "../utils/cadStudioLighting";
import { clipCadMesh } from "../utils/cadClip";
import { bindCadAnimationTracks, CAD_ANIMATION_ACTIONS, CAD_ANIMATION_SCHEMA, canonicalCadAnimationJson,
  normalizeCadAnimation, sampleCadAnimationTrack } from "../utils/cadAnimation";
import type { BoundCadAnimationTrack, CadAnimationAction, CadAnimationDocument } from "../utils/cadAnimation";
import { buildZebraPixels, DEFAULT_ZEBRA_COLORS, normalizeZebraAngle, normalizeZebraMapping, normalizeZebraScheme, zebraDirectionVector, ZEBRA_TEXTURE_HEIGHT } from "../utils/cadZebra";
import type { ZebraColors, ZebraMapping, ZebraScheme } from "../utils/cadZebra";
import { decodeCadStudioTexture, normalizeCadStudioMapping, setCadStudioTexture } from "../utils/cadStudioTexture";
import type { CadStudioMapping } from "../utils/cadStudioTexture";
import GeometryDownloads from './GeometryDownloads';
import GeometryValidation from './GeometryValidation';
import CadObjectTree from './CadObjectTree';
import CadStudioLightingControls from './CadStudioLightingControls';
import type { CadObjectState } from './CadObjectTree';
import { positivePreviewTolerance, readCadTree, readCaseSelection, saveCadTree, saveCaseSelection } from "../utils/viewSelection";
import type { CadTreePreferences } from "../utils/viewSelection";
import './GeoPanel.css';

declare global {
  interface Window {
    vfLoadSceneFromBase64?: (encoded: string) => void;
    vfFit?: () => void;
    vfSetEdges?: (enabled: boolean) => void;
    vfSetTransparent?: (enabled: boolean) => void;
    setVibeFlowTheme?: (theme: string) => void;
  }
}

type CadViewSource = "case" | "assembly" | "local";
type CadViewedObject = CadObjectState & {hasUV: boolean};
const CAD_GRID_PLANES = ["xz", "xy", "yz"] as const;
const CAD_QUAD_PRESETS = ["iso", "x", "y", "z"] as const;
const DEFAULT_SURFACE = CAD_SURFACES.standard.values;

function makeCadGrids(dark: boolean) {
  const make = () => new THREE.GridHelper(200, 20, dark ? 0x2a2f3a : 0x94a3b8, dark ? 0x1c2230 : 0xd5dbe3);
  const grids = {xz: make(), xy: make(), yz: make()};
  grids.xy.rotation.x = Math.PI / 2;
  grids.yz.rotation.z = Math.PI / 2;
  return grids;
}

function disposeCadGrids(grids: ReturnType<typeof makeCadGrids>) {
  for (const grid of Object.values(grids)) {
    grid.removeFromParent();
    grid.geometry.dispose();
    grid.material.dispose();
  }
}
type ExplodeEasing = "linear" | "ease-in-out" | "ease-out";
type ExplodePhase = "idle" | "playing" | "paused";
type CadTrackBinding = {document: CadAnimationDocument; tracks: BoundCadAnimationTrack[]};
interface CadJobWatch { timer: number | null; generation: number }
const emptyTree: CadTreePreferences = {hidden: [], collapsed: [], selected: ""};
const drawerDefault = () => !window.matchMedia('(max-width: 760px)').matches;

const DEFAULT_TRACK_TEXT = JSON.stringify({
  schema: CAD_ANIMATION_SCHEMA, speed: 1, repeat: false,
  tracks: [
    {selector: "/auto", action: "t", times: [0, 1], values: [[0, 0, 0], [1, 2, 3]]},
    {selector: "/auto", action: "tx", times: [0, 1], values: [0, 0.5]},
    {selector: "/auto", action: "ty", times: [0, 1], values: [0, 1]},
    {selector: "/auto", action: "tz", times: [0, 1], values: [0, 1.5]},
    {selector: "/auto", action: "q", times: [0, 1], values: [[0, 0, 0, 1], [0, 0, 0.7071067811865476, 0.7071067811865476]]},
    {selector: "/auto", action: "rx", times: [0, 1], values: [0, 30]},
    {selector: "/auto", action: "ry", times: [0, 1], values: [0, 60]},
    {selector: "/auto", action: "rz", times: [0, 1], values: [0, 90]},
  ],
}, null, 2);
const CAD_TRACK_ACTION_LABELS: Record<CadAnimationAction, string> = {
  t: "三轴平移", tx: "X 平移", ty: "Y 平移", tz: "Z 平移",
  q: "四元数旋转", rx: "X 旋转", ry: "Y 旋转", rz: "Z 旋转",
};

const ZEBRA_VERTEX_SHADER = `
  varying vec3 vViewNormal;
  varying vec3 vViewPosition;
  varying vec4 vScreenPosition;
  void main() {
    vViewNormal = normalize(normalMatrix * normal);
    vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
    vViewPosition = mvPosition.xyz;
    gl_Position = projectionMatrix * mvPosition;
    vScreenPosition = gl_Position;
  }
`;

const ZEBRA_FRAGMENT_SHADER = `
  uniform sampler2D zebraTexture;
  uniform vec3 direction;
  uniform float opacity;
  uniform vec3 baseColor;
  uniform float alpha;
  uniform int mappingMode;
  varying vec3 vViewNormal;
  varying vec3 vViewPosition;
  varying vec4 vScreenPosition;
  void main() {
    vec3 normal = normalize(vViewNormal);
    float v;
    if (mappingMode == 0) {
      vec3 viewDir = normalize(-vViewPosition);
      vec3 mappingVector = reflect(-viewDir, normal);
      v = dot(mappingVector, direction) * 3.0 * 0.5 + 0.5;
    } else {
      float dist = length(vViewPosition);
      vec2 viewDir2D = vViewPosition.xy / dist;
      float cosA = direction.x / length(direction.xy);
      float sinA = direction.y / length(direction.xy);
      float rotatedPos = viewDir2D.x * cosA + viewDir2D.y * sinA;
      float positionValue = rotatedPos * 2.0;
      float normalValue = dot(normal, direction) * 0.5;
      v = (positionValue + normalValue) * 3.0 * 0.5 + 0.5;
    }
    vec4 zebraColor = texture2D(zebraTexture, vec2(0.5, v));
    vec3 finalColor = mix(baseColor, zebraColor.rgb, opacity);
    gl_FragColor = vec4(finalColor, alpha);
  }
`;

/** 几何预览面板：CadQuery scene.json → three.js 渲染 */
type PreviewGeometryDocument = CadSceneDocument & {
  tolerance?: unknown;
  ocp_status?: {status?: unknown; reason?: unknown};
};

const DEFAULT_TOLERANCE = 0.1;

function previewGeometryMetadata(document: PreviewGeometryDocument) {
  const tolerance = positivePreviewTolerance(document.tolerance) ? document.tolerance : undefined;
  const ocp = document.ocp_status;
  let ocpLabel = "";
  if (ocp?.status === "ready") ocpLabel = "OCP 几何数据就绪";
  if (ocp?.status === "fallback") {
    if (ocp.reason === "ocp_unavailable") ocpLabel = "三角网格回退（OCP 不可用）";
    if (ocp.reason === "ocp_conversion_failed") ocpLabel = "三角网格回退（OCP 转换失败）";
  }
  return {tolerance, ocpLabel};
}

export default function GeoPanel() {
  const { currentCase, user, theme: appTheme } = useStore();
  const autoPreviewHandoff = useStore(state => state.currentCase && state.autoPreviewHandoffs[state.currentCase.id]);
  const autoPreviewJobId = autoPreviewHandoff?.jobId;
  const autoPreviewError = autoPreviewHandoff?.error;
  const [sceneLoadError, setSceneLoadError] = useState("");
  const [toleranceDraft, setToleranceDraft] = useState(String(DEFAULT_TOLERANCE));
  const [previewMetadata, setPreviewMetadata] = useState<ReturnType<typeof previewGeometryMetadata>>();
  const ownTolerance = useRef(false);
  const chosenTolerance = Number(toleranceDraft);
  const invalidTolerance = !positivePreviewTolerance(chosenTolerance);
  const filePreview = useStore(state => state.filePreviewRequest);
  const mountRef = useRef<HTMLDivElement>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const gridsRef = useRef<ReturnType<typeof makeCadGrids> | null>(null);
  const [gridPlanes, setGridPlanes] = useState(DEFAULT_CAD_GRID);
  const gridPlanesRef = useRef(DEFAULT_CAD_GRID);
  const [gridCentered, setGridCentered] = useState(false);
  const gridCenteredRef = useRef(false);
  const controlsRef = useRef<OrbitControls | TrackballControls | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | THREE.OrthographicCamera | null>(null);
  const perspectiveCameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const orthoCameraRef = useRef<THREE.OrthographicCamera | null>(null);
  const quadViewsRef = useRef<CadQuadView[]>([]);
  const quadElementsRef = useRef<(HTMLDivElement | null)[]>([]);
  const activeQuadRef = useRef(0);
  const [activeQuad, setActiveQuad] = useState(0);
  const [quadControls, setQuadControls] = useState<{projection: boolean; mode: CadControl; preset: CadViewPreset}>(
    {projection: false, mode: "orbit", preset: "iso"});
  const viewportAspectRef = useRef(1);
  const ambientLightRef = useRef<THREE.AmbientLight | null>(null);
  const directLightRef = useRef<THREE.DirectionalLight | null>(null);
  const environmentRef = useRef<CadStudioEnvironment | null>(null);
  const environmentRequest = useRef<AbortController | null>(null);
  const gradientTextures = useRef<{gradient: THREE.Texture; "gradient-dark": THREE.Texture} | null>(null);
  const shadowPlaneRef = useRef<THREE.Mesh | null>(null);
  const shadowRef = useRef(false);
  const composerRef = useRef<EffectComposer | null>(null);
  const ssaoPassRef = useRef<SSAOPass | null>(null);
  const aoRef = useRef(false);
  const previewWatch = useRef<CadJobWatch>({timer: null, generation: 0});
  const exportWatch = useRef<CadJobWatch>({timer: null, generation: 0});
  const exportSelection = useRef<ReturnType<typeof captureProjectCase>>(null);
  const sceneReadRef = useRef(0);
  const viewingStep = useRef(false);
  const [status, setStatus] = useState("");
  const [exportStatus, setExportStatus] = useState("");
  const [exportBusy, setExportBusy] = useState(false);
  const [objInfo, setObjInfo] = useState("");
  const [objects, setObjects] = useState<CadViewedObject[]>([]);
  const [selectedName, setSelectedName] = useState("");
  const [compareName, setCompareName] = useState("");
  const [treeOpen, setTreeOpen] = useState(drawerDefault);
  const [collapsedGroups, setCollapsedGroups] = useState<string[]>([]);
  const treeMemory = useRef<{source: CadViewSource; owner: string; prefs: CadTreePreferences;
    selection: ReturnType<typeof captureProjectCase>} | null>(null);
  const drawnScene = useRef<typeof treeMemory.current>(null);
  const caseGeometry = useRef<{selection: NonNullable<ReturnType<typeof captureProjectCase>>;
    ordinary: CadSceneDocument; assembly: unknown} | null>(null);
  const [hasAssembly, setHasAssembly] = useState(false);
  const [assemblyOpen, setAssemblyOpen] = useState(false);
  const [edgesOn, setEdgesOn] = useState(true);
  const [transparentOn, setTransparentOn] = useState(false);
  const [theme, setTheme] = useState<CadTheme>("dark");
  const edgesRef = useRef(true);
  const transparentRef = useRef(false);
  const themeRef = useRef<CadTheme>("dark");
  const [orthoOn, setOrthoOn] = useState(false);
  const [controlMode, setControlMode] = useState<CadControl>("orbit");
  const [viewMode, setViewMode] = useState<CadViewMode>("single");
  const viewModeRef = useRef<CadViewMode>("single");
  const [studioOn, setStudioOn] = useState(false);
  const studioEnabled = useRef(false);
  const [studioLighting, setStudioLighting] = useState(DEFAULT_CAD_STUDIO);
  const studioLightingRef = useRef(DEFAULT_CAD_STUDIO);
  const [environmentStatus, setEnvironmentStatus] = useState("");
  const [environmentSize, setEnvironmentSize] = useState<[number, number] | null>(null);
  const studioMapping = useRef<CadStudioMapping>("triplanar");
  const [studioMappingMode, setStudioMappingMode] = useState<CadStudioMapping>("triplanar");
  const studioImage = useRef<Awaited<ReturnType<typeof decodeCadStudioTexture>> | null>(null);
  const studioImageRequest = useRef(0);
  const [studioImageName, setStudioImageName] = useState("");
  const [studioImageStatus, setStudioImageStatus] = useState("");
  const [shadowOn, setShadowOn] = useState(false);
  const [aoOn, setAoOn] = useState(false);
  const [materialSettings, setMaterialSettings] = useState<CadMaterialSettings>(() => normalizeCadMaterial({}));
  const surfaceRef = useRef<CadSurfaceSettings>(DEFAULT_SURFACE);
  const surfaceOverrides = useRef(new Map<string, CadSurfaceSettings>());
  const [, refreshSurfaceControls] = useState(0);
  const controlRef = useRef<CadControl>("orbit");
  const materialRef = useRef(materialSettings);
  const applySceneRef = useRef<(document: CadSceneDocument, source?: CadViewSource) => void>(() => {});
  const loadSceneRef = useRef<() => Promise<void>>(async () => {});
  const [explodeOn, setExplodeOn] = useState(false);
  const [explodeAmount, setExplodeAmount] = useState(0.5);
  const [explodeAnimated, setExplodeAnimated] = useState(true);
  const [explodeDurationDraft, setExplodeDurationDraft] = useState("0.5");
  const [explodeDuration, setExplodeDuration] = useState(0.5);
  const [explodeEasing, setExplodeEasing] = useState<ExplodeEasing>("linear");
  const [explodePhase, setExplodePhase] = useState<ExplodePhase>("idle");
  const [reduceExplodeMotion, setReduceExplodeMotion] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [explodeProgress, setExplodeProgress] = useState(0);
  const explodeMotionRef = useRef({
    current: 0, from: 0, target: 0, elapsed: 0, reported: 0,
    duration: 0.5, easing: "linear" as ExplodeEasing, phase: "idle" as ExplodePhase,
  });
  const validExplodeDuration = explodeDurationDraft.trim() !== "" &&
    Number(explodeDurationDraft) >= 0.1 && Number(explodeDurationDraft) <= 10;
  const [clipAxis, setClipAxis] = useState("");
  const [clipSide, setClipSide] = useState("lower");
  const [clipPos, setClipPos] = useState(0.5);
  const [clipCapCount, setClipCapCount] = useState(0);
  const [clipOpenContours, setClipOpenContours] = useState(false);
  const [zebraOn, setZebraOn] = useState(false);
  const [zebraCount, setZebraCount] = useState(12);
  const [zebraCountDraft, setZebraCountDraft] = useState("12");
  const [zebraOpacity, setZebraOpacity] = useState(0.35);
  const [zebraScheme, setZebraScheme] = useState<ZebraScheme>("blackwhite");
  const [zebraColors, setZebraColors] = useState<ZebraColors>(DEFAULT_ZEBRA_COLORS);
  const [zebraMapping, setZebraMapping] = useState<ZebraMapping>("normal");
  const [zebraAngle, setZebraAngle] = useState(0);
  const zebraMaterialsRef = useRef(new Map<string, THREE.ShaderMaterial>());
  const zebraTextureKeyRef = useRef("");
  const [animOn, setAnimOn] = useState(false);
  const [animRx, setAnimRx] = useState(0);
  const [animRy, setAnimRy] = useState(1);
  const [animRz, setAnimRz] = useState(0);
  const [animTx, setAnimTx] = useState(0);
  const [animTy, setAnimTy] = useState(0);
  const [animTz, setAnimTz] = useState(0);
  const [animRepeat, setAnimRepeat] = useState(true);
  const animTimeRef = useRef(0);
  const animStateRef = useRef({ on: false, rx: 0, ry: 1, rz: 0, tx: 0, ty: 0, tz: 0, repeat: true });
  animStateRef.current = { on: animOn, rx: animRx, ry: animRy, rz: animRz,
    tx: animTx, ty: animTy, tz: animTz, repeat: animRepeat };
  const [trackText, setTrackText] = useState(DEFAULT_TRACK_TEXT);
  const trackInputRef = useRef<HTMLTextAreaElement>(null);
  const [trackAction, setTrackAction] = useState<CadAnimationAction>("t");
  const [trackDoc, setTrackDoc] = useState<CadAnimationDocument | null>(null);
  const trackBindingRef = useRef<CadTrackBinding | null>(null);
  const trackPoseRef = useRef(false);
  const [trackPlaying, setTrackPlaying] = useState(false);
  const trackPlayingRef = useRef(false);
  const [trackTime, setTrackTime] = useState(0);
  const trackTimeRef = useRef(0);
  const [trackStatus, setTrackStatus] = useState("");
  const sceneCenterRef = useRef(new THREE.Vector3());
  const sceneSizeRef = useRef(new THREE.Vector3(1, 1, 1));
  const clipHelperRef = useRef<THREE.Mesh | null>(null);
  const zebraTextureRef = useRef<THREE.DataTexture | null>(null);

  // 初始化渲染器
  useEffect(() => {
    const host = mountRef.current!;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    host.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0f1115);
    const ambient = new THREE.AmbientLight(0xffffff, materialRef.current.ambientIntensity);
    ambientLightRef.current = ambient;
    scene.add(ambient);
    const dir = new THREE.DirectionalLight(0xffffff, materialRef.current.directIntensity);
    directLightRef.current = dir;
    dir.position.set(3, 5, 4);
    scene.add(dir);
    const grids = makeCadGrids(true);
    gridsRef.current = grids;
    scene.add(grids.xz, grids.xy, grids.yz);
    updateCadGrid();
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
    perspectiveCameraRef.current = camera;
    cameraRef.current = camera;
    camera.position.set(120, 90, 120);
    const controls = new OrbitControls(camera, renderer.domElement);
    controlsRef.current = controls;
    const ro = new ResizeObserver(() => {
      const w = host.clientWidth, h = host.clientHeight;
      if (w && h) {
        drawnScene.current = null;
        const aspect = w / h;
        viewportAspectRef.current = aspect;
        renderer.setSize(w, h);
        composerRef.current?.setSize(w, h);
        camera.aspect = aspect;
        camera.updateProjectionMatrix();
        if (controlsRef.current instanceof TrackballControls) controlsRef.current.handleResize();
        const active = cameraRef.current;
        if (active instanceof THREE.OrthographicCamera) {
          updateOrthographicProjection(active, cadOrthoSpan(sceneSizeRef.current.toArray(), aspect));
        }
      }
    });
    ro.observe(host);
    rendererRef.current = renderer;
    sceneRef.current = scene;
    let raf = 0;
    const clock = new THREE.Clock();
    const resetVisualClock = () => { clock.getDelta(); };
    document.addEventListener("visibilitychange", resetVisualClock);
    const loop = () => {
      const frameDelta = clock.getDelta();
      const delta = Math.min(frameDelta, 0.05);
      const anim = animStateRef.current;
      if (anim.on) {
        animTimeRef.current += delta;
        const t = animTimeRef.current;
        for (const child of scene.children) {
          if (!child.userData.vfMesh) continue;
          child.rotation.set(child.userData.baseRotation.x + anim.rx * t,
                             child.userData.baseRotation.y + anim.ry * t,
                             child.userData.baseRotation.z + anim.rz * t);
          child.position.copy(child.userData.home).add(new THREE.Vector3(anim.tx * t, anim.ty * t, anim.tz * t));
        }
        if (t >= 1) {
          if (anim.repeat) resetAnimation();
          else {
            animStateRef.current.on = false;
            setAnimOn(false);
            resetAnimation();
          }
        }
      }
      const trackDocument = trackBindingRef.current?.document;
      if (trackPlayingRef.current && trackDocument) {
        let next = trackTimeRef.current + delta * trackDocument.speed;
        if (next >= trackDocument.duration) {
          if (trackDocument.repeat) next %= trackDocument.duration;
          else {
            trackPlayingRef.current = false;
            trackPoseRef.current = false;
            setTrackPlaying(false);
            resetAnimation();
            trackTimeRef.current = 0;
            setTrackTime(0);
            setTrackStatus("轨道播放完成，已恢复源位置");
            next = -1;
          }
        }
        if (next >= 0) applyTrackAtTime(next);
      }
      const motion = explodeMotionRef.current;
      if (motion.phase === "playing" && !document.hidden) {
        motion.elapsed = Math.min(motion.elapsed + frameDelta, motion.duration);
        const t = motion.elapsed / motion.duration;
        const eased = motion.easing === "ease-in-out" ? t * t * (3 - 2 * t)
          : motion.easing === "ease-out" ? 1 - (1 - t) ** 3 : t;
        motion.current = t === 1 ? motion.target : THREE.MathUtils.lerp(motion.from, motion.target, eased);
        applyExplode();
        if (Math.abs(motion.current - motion.reported) > 0.005 || t === 1) {
          motion.reported = motion.current;
          setExplodeProgress(Number(motion.current.toFixed(3)));
        }
        if (t === 1) changeExplodePhase("idle");
      }
      controlsRef.current?.update();
      if (cameraRef.current) {
        if (viewModeRef.current === "quad") renderQuad(scene, renderer, host);
        else if (aoRef.current && composerRef.current) {
          updateAOCamera(cameraRef.current);
          composerRef.current.render();
        } else renderer.render(scene, cameraRef.current);
        drawnScene.current = treeMemory.current;
      }
      raf = requestAnimationFrame(loop);
    };
    loop();
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange", resetVisualClock);
      drawnScene.current = null;
      ro.disconnect();
      controlsRef.current?.dispose();
      controlsRef.current = null;
      quadViewsRef.current.forEach(view => view.detach());
      if (gridsRef.current) disposeCadGrids(gridsRef.current);
      gridsRef.current = null;
      environmentRequest.current?.abort();
      environmentRequest.current = null;
      environmentRef.current?.dispose();
      environmentRef.current = null;
      Object.values(gradientTextures.current ?? {}).forEach(texture => texture.dispose());
      gradientTextures.current = null;
      ++studioImageRequest.current;
      studioImage.current?.dispose();
      studioImage.current = null;
      composerRef.current?.passes.forEach(pass => pass.dispose());
      composerRef.current?.dispose();
      composerRef.current = null;
      ssaoPassRef.current = null;
      if (shadowPlaneRef.current) {
        shadowPlaneRef.current.geometry.dispose();
        (shadowPlaneRef.current.material as THREE.Material).dispose();
        shadowPlaneRef.current = null;
      }
      zebraTextureRef.current?.dispose();
      zebraTextureRef.current = null;
      zebraTextureKeyRef.current = "";
      zebraMaterialsRef.current.forEach(material => material.dispose());
      zebraMaterialsRef.current.clear();
      renderer.dispose();
      host.removeChild(renderer.domElement);
    };
  }, []);

  const updateOrthographicProjection = (camera: THREE.OrthographicCamera, span: number) => {
    const aspect = viewportAspectRef.current;
    camera.left = -span * aspect / 2;
    camera.right = span * aspect / 2;
    camera.top = span / 2;
    camera.bottom = -span / 2;
    camera.updateProjectionMatrix();
  };

  const recreateControls = (camera: THREE.PerspectiveCamera | THREE.OrthographicCamera) => {
    const renderer = rendererRef.current;
    if (!renderer) return;
    const target = controlsRef.current?.target.clone() ?? new THREE.Vector3();
    controlsRef.current?.dispose();
    const controls = makeCadControls(camera, renderer.domElement, controlRef.current);
    controls.target.copy(target);
    controls.enabled = viewModeRef.current === "single";
    controlsRef.current = controls;
    controls.update();
  };

  const setControlPreference = (value: unknown) => {
    const normalized = normalizeCadControl(value);
    if (viewModeRef.current === "quad") {
      quadViewsRef.current[activeQuadRef.current]?.setMode(normalized);
      refreshQuadSelection(activeQuadRef.current);
      return;
    }
    controlRef.current = normalized;
    setControlMode(normalized);
    if (cameraRef.current) recreateControls(cameraRef.current);
  };

  const setOrthoPreference = (enabled: boolean) => {
    if (viewModeRef.current === "quad") {
      quadViewsRef.current[activeQuadRef.current]?.setProjection(enabled);
      refreshQuadSelection(activeQuadRef.current);
      return;
    }
    setOrthoOn(enabled);
    const current = cameraRef.current;
    if (!current || enabled === (current instanceof THREE.OrthographicCamera)) return;
    if (enabled) {
      const next = orthoCameraRef.current ?? new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 5000);
      orthoCameraRef.current = next;
      next.position.copy(current.position);
      next.quaternion.copy(current.quaternion);
      next.up.copy(current.up);
      next.zoom = 1;
      cameraRef.current = next;
    } else {
      const next = perspectiveCameraRef.current;
      if (!next) return;
      next.position.copy(current.position);
      next.quaternion.copy(current.quaternion);
      next.up.copy(current.up);
      next.zoom = 1;
      cameraRef.current = next;
    }
    const active = cameraRef.current;
    if (active) {
      recreateControls(active);
      fitScene();
    }
  };

  const applyMaterial = () => {
    const settings = materialRef.current;
    if (ambientLightRef.current) ambientLightRef.current.intensity = settings.ambientIntensity;
    if (directLightRef.current) directLightRef.current.intensity = settings.directIntensity;
    sceneRef.current?.children.forEach((child) => {
      if (!(child instanceof THREE.Mesh) || !child.userData.vfMesh) return;
      const material = child.userData.originalMaterial as THREE.MeshPhysicalMaterial;
      const surface = surfaceOverrides.current.get(child.userData.vfObjectKey) ?? surfaceRef.current;
      material.setValues({metalness: settings.metalness, roughness: settings.roughness, ...surface});
      material.needsUpdate = true;
    });
  };

  const selectedSurface = () => {
    const object = objects.find(item => item.name === selectedName);
    return (object && surfaceOverrides.current.get(object.key)) ?? surfaceRef.current;
  };

  const changeSurface = (settings: CadSurfaceSettings) => {
    const next = normalizeCadSurface(settings);
    const object = objects.find(item => item.name === selectedName);
    if (object) surfaceOverrides.current.set(object.key, next);
    else surfaceRef.current = next;
    applyMaterial();
    refreshSurfaceControls(value => value + 1);
  };

  const setMaterialValue = (key: keyof CadMaterialSettings, value: number) => {
    const next = normalizeCadMaterial({...materialRef.current, [key]: value});
    materialRef.current = next;
    setMaterialSettings(next);
    applyMaterial();
  };

  const applyStudioTextures = () => {
    const texture = studioEnabled.current ? studioImage.current?.texture ?? null : null;
    sceneRef.current?.children.forEach(child => {
      if (!(child instanceof THREE.Mesh) || !child.userData.vfMesh) return;
      const material = child.userData.originalMaterial as THREE.MeshStandardMaterial;
      setCadStudioTexture(material, child.geometry, texture, studioMapping.current, child.userData.sourceGeometry);
    });
  };

  const repaintStudio = () => {
    const scene = sceneRef.current;
    const renderer = rendererRef.current;
    if (!scene || !renderer) return;
    gradientTextures.current ??= {gradient: cadStudioGradient(false), "gradient-dark": cadStudioGradient(true)};
    applyCadStudioLighting(renderer, scene, studioEnabled.current, studioLightingRef.current, environmentRef.current,
      gradientTextures.current, themeRef.current === "dark");
    const pass = ssaoPassRef.current;
    if (pass) pass.copyMaterial.uniforms.cadAOIntensity.value = studioLightingRef.current.aoIntensity;
    applyShadows(shadowRef.current);
  };

  const commitStudioLighting = (settings: CadStudioLighting) => {
    studioLightingRef.current = settings;
    setStudioLighting(settings);
  };

  const refreshEnvironment = async (settings: CadStudioLighting) => {
    environmentRequest.current?.abort();
    const renderer = rendererRef.current;
    if (!renderer || !studioEnabled.current) return;
    const prior = environmentRef.current;
    if (prior?.preset === settings.environment && (prior.preset === "studio" || prior.highResolution === settings.highResolution)) {
      setEnvironmentStatus("");
      return;
    }
    const request = new AbortController();
    environmentRequest.current = request;
    try {
      if (settings.environment !== "studio") setEnvironmentStatus("正在加载环境图…");
      const next = settings.environment === "studio" ? proceduralCadEnvironment(renderer)
        : await loadCadEnvironment(settings.environment, settings.highResolution, renderer.capabilities.maxTextureSize, request.signal);
      if (request.signal.aborted) {
        next.dispose();
        return;
      }
      environmentRef.current = next;
      setEnvironmentSize(next.size);
      setEnvironmentStatus("");
      repaintStudio();
      prior?.dispose();
    } catch (error) {
      if (request.signal.aborted) return;
      commitStudioLighting({...studioLightingRef.current,
        environment: prior?.preset ?? "studio", highResolution: prior?.highResolution ?? false});
      setEnvironmentStatus(error instanceof Error ? error.message : String(error));
      repaintStudio();
    }
  };

  const changeStudioLighting = (patch: Partial<CadStudioLighting>) => {
    const next = {...studioLightingRef.current, ...patch};
    commitStudioLighting(next);
    if ("environment" in patch || "highResolution" in patch) void refreshEnvironment(next);
    else repaintStudio();
  };

  const applyStudio = (enabled: boolean) => {
    studioEnabled.current = enabled;
    setStudioOn(enabled);
    if (enabled) void refreshEnvironment(studioLightingRef.current);
    else {
      environmentRequest.current?.abort();
      setEnvironmentStatus("");
    }
    repaintStudio();
    applyStudioTextures();
  };

  const updateStudioMapping = (value: string) => {
    const mapping = normalizeCadStudioMapping(value);
    studioMapping.current = mapping;
    setStudioMappingMode(mapping);
    applyStudioTextures();
  };

  const replaceStudioImage = (next: typeof studioImage.current, name = "") => {
    const previous = studioImage.current;
    studioImage.current = next;
    setStudioImageName(name);
    setStudioImageStatus("");
    applyStudioTextures();
    previous?.dispose();
  };

  const loadStudioImage = async (file: File) => {
    const selection = captureProjectCase();
    if (!selection) return;
    const sequence = ++studioImageRequest.current;
    const current = () => sequence === studioImageRequest.current && selection.isCurrent();
    setStudioImageStatus("正在读取纹理…");
    try {
      const next = await decodeCadStudioTexture(file);
      if (current()) replaceStudioImage(next, file.name);
      else next.dispose();
    } catch (error) {
      if (current()) setStudioImageStatus(error instanceof Error ? error.message : String(error));
    }
  };

  const updateShadowPlane = () => {
    const center = sceneCenterRef.current;
    const size = sceneSizeRef.current;
    if (shadowPlaneRef.current) {
      shadowPlaneRef.current.position.set(center.x, center.y - size.y / 2 - 0.02, center.z);
    }
    const light = directLightRef.current;
    if (light) {
      const span = Math.max(size.length(), 10);
      light.shadow.camera.left = -span;
      light.shadow.camera.right = span;
      light.shadow.camera.top = span;
      light.shadow.camera.bottom = -span;
      light.shadow.camera.far = Math.max(100, span * 4);
      light.shadow.camera.updateProjectionMatrix();
      light.shadow.mapSize.set(1024, 1024);
      light.shadow.bias = -0.0005;
    }
  };

  const applyShadows = (enabled: boolean) => {
    shadowRef.current = enabled;
    setShadowOn(enabled);
    const scene = sceneRef.current;
    const renderer = rendererRef.current;
    if (!scene || !renderer) return;
    const studio = studioEnabled.current;
    const studioSettings = studioLightingRef.current;
    renderer.shadowMap.enabled = enabled;
    renderer.shadowMap.type = studio ? THREE.PCFShadowMap : THREE.PCFSoftShadowMap;
    const light = directLightRef.current;
    if (light) {
      light.castShadow = enabled;
      light.shadow.intensity = studio ? studioSettings.shadowIntensity : 1;
      light.shadow.radius = studio ? 1 + studioSettings.shadowSoftness * 7 : 1;
    }
    scene.children.forEach((child) => {
      if (child instanceof THREE.Mesh && child.userData.vfMesh) {
        child.castShadow = enabled;
        child.receiveShadow = enabled;
        (child.material as THREE.MeshStandardMaterial).needsUpdate = true;
      }
    });
    if (enabled && !shadowPlaneRef.current) {
      const plane = new THREE.Mesh(new THREE.PlaneGeometry(1000, 1000),
        new THREE.ShadowMaterial({color: 0x000000, opacity: 0.25}));
      plane.rotation.x = -Math.PI / 2;
      plane.receiveShadow = true;
      shadowPlaneRef.current = plane;
      scene.add(plane);
    }
    if (shadowPlaneRef.current) {
      shadowPlaneRef.current.visible = enabled;
      (shadowPlaneRef.current.material as THREE.ShadowMaterial).opacity = studio ? 1 : 0.25;
    }
    updateShadowPlane();
  };

  const updateAOCamera = (camera: THREE.PerspectiveCamera | THREE.OrthographicCamera) => {
    const pass = ssaoPassRef.current;
    if (!pass) return;
    pass.camera = camera;
    const perspective = camera instanceof THREE.PerspectiveCamera ? 1 : 0;
    for (const material of [pass.ssaoMaterial, pass.depthRenderMaterial]) {
      if (material.defines.PERSPECTIVE_CAMERA !== perspective) {
        material.defines.PERSPECTIVE_CAMERA = perspective;
        material.needsUpdate = true;
      }
    }
    pass.ssaoMaterial.uniforms.cameraNear.value = camera.near;
    pass.ssaoMaterial.uniforms.cameraFar.value = camera.far;
    pass.ssaoMaterial.uniforms.cameraProjectionMatrix.value.copy(camera.projectionMatrix);
    pass.ssaoMaterial.uniforms.cameraInverseProjectionMatrix.value.copy(camera.projectionMatrixInverse);
    const renderPass = composerRef.current?.passes[0] as RenderPass | undefined;
    if (renderPass) renderPass.camera = camera;
  };

  const applyAO = (enabled: boolean) => {
    aoRef.current = enabled;
    setAoOn(enabled);
    const scene = sceneRef.current;
    const renderer = rendererRef.current;
    const camera = cameraRef.current;
    const host = mountRef.current;
    if (!scene || !renderer || !camera || !host) return;
    if (enabled && viewModeRef.current === "quad") setViewModePreference("single");
    if (enabled && !composerRef.current) {
      const composer = new EffectComposer(renderer);
      composer.addPass(new RenderPass(scene, camera));
      const ssao = new SSAOPass(scene, camera, Math.max(host.clientWidth, 1), Math.max(host.clientHeight, 1), 16);
      ssao.kernelRadius = 0.5;
      ssao.minDistance = 0;
      ssao.maxDistance = 1;
      ssao.output = 0;
      ssao.enabled = true;
      const copy = ssao.copyMaterial;
      copy.uniforms.cadAOIntensity = {value: studioLightingRef.current.aoIntensity};
      copy.fragmentShader = "uniform float cadAOIntensity;\n" + copy.fragmentShader.replace(
        "gl_FragColor = opacity * texel;",
        "gl_FragColor = vec4(pow(max(texel.rgb, vec3(0.001)), vec3(cadAOIntensity)), 1.0);");
      composer.addPass(ssao);
      composer.addPass(new OutputPass());
      composerRef.current = composer;
      ssaoPassRef.current = ssao;
    }
    if (composerRef.current) updateAOCamera(camera);
  };

  const refreshQuadSelection = (index: number) => {
    const view = quadViewsRef.current[index];
    if (!view) return;
    activeQuadRef.current = index;
    setActiveQuad(index);
    setQuadControls(previous => previous.mode === view.mode && previous.projection === view.orthographic && previous.preset === view.preset
      ? previous : {mode: view.mode, projection: view.orthographic, preset: view.preset});
  };

  const ensureQuadViews = () => {
    const host = mountRef.current;
    if (!host || host.clientWidth < 2 || host.clientHeight < 2 || quadViewsRef.current.length) return;
    quadViewsRef.current = cadQuadViewports(host.clientWidth, host.clientHeight).map(viewport => {
      const view = new CadQuadView(viewport.preset);
      view.resize(viewport.width, viewport.height);
      view.fit(sceneCenterRef.current, sceneSizeRef.current);
      return view;
    });
    quadViewsRef.current.forEach((view, index) => {
      const element = quadElementsRef.current[index];
      if (element) view.attach(element);
    });
  };

  const setViewModePreference = (value: unknown) => {
    const normalized = normalizeCadViewMode(value);
    if (normalized === "quad" && aoRef.current) applyAO(false);
    viewModeRef.current = normalized;
    setViewMode(normalized);
    if (normalized === "quad") {
      ensureQuadViews();
      refreshQuadSelection(activeQuadRef.current);
    }
    if (controlsRef.current) controlsRef.current.enabled = normalized === "single";
  };

  const renderQuad = (scene: THREE.Scene, renderer: THREE.WebGLRenderer, host: HTMLDivElement) => {
    if (host.clientWidth < 2 || host.clientHeight < 2) return;
    ensureQuadViews();
    const views = quadViewsRef.current;
    if (views.length !== 4) return;
    const viewports = cadQuadViewports(host.clientWidth, host.clientHeight);
    renderer.setScissorTest(true);
    viewports.forEach((viewport, index) => {
      const view = views[index];
      view.resize(viewport.width, viewport.height);
      view.update();
      renderer.setViewport(viewport.x, viewport.y, viewport.width, viewport.height);
      renderer.setScissor(viewport.x, viewport.y, viewport.width, viewport.height);
      renderer.render(scene, view.camera);
    });
    renderer.setScissorTest(false);
    const size = renderer.getSize(new THREE.Vector2());
    renderer.setViewport(0, 0, size.x, size.y);
  };

  const fitScene = (activeViewOnly = false) => {
    const scene = sceneRef.current;
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!scene || !camera || !controls) return;
    const box = new THREE.Box3();
    for (const child of scene.children) {
      if (!(child instanceof THREE.Mesh) || !child.userData.vfMesh || !child.visible) continue;
      child.updateWorldMatrix(true, false);
      box.expandByObject(child);
    }
    if (box.isEmpty()) return;
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    if (activeViewOnly && viewModeRef.current === "quad") {
      quadViewsRef.current[activeQuadRef.current]?.fit(center, size);
      return;
    }
    let distance: number;
    if (camera instanceof THREE.PerspectiveCamera) {
      distance = cadFitDistance(size.toArray(), camera.fov, camera.aspect || 1);
    } else {
      updateOrthographicProjection(camera, cadOrthoSpan(size.toArray(), viewportAspectRef.current));
      distance = Math.max(size.length() * 2, 1);
    }
    camera.zoom = 1;
    camera.far = Math.max(50, distance * 4);
    const differentUp = camera.up.y !== 1 || camera.up.z !== 0;
    camera.up.set(0, 1, 0);
    camera.updateProjectionMatrix();
    camera.position.copy(center).add(new THREE.Vector3(1, 0.7, 1).normalize().multiplyScalar(distance));
    controls.target.copy(center);
    if (differentUp) recreateControls(camera);
    else controls.update();
    if (viewModeRef.current === "quad") ensureQuadViews();
    quadViewsRef.current.forEach(view => view.fit(center, size));
  };

  const applyEdges = () => {
    const scene = sceneRef.current;
    if (!scene) return;
    for (const child of scene.children) {
      if (!(child instanceof THREE.LineSegments) || !child.userData.vfMesh) continue;
      const owner = scene.children.find((item) =>
        item instanceof THREE.Mesh && item.userData.vfMesh === child.userData.vfMesh);
      child.visible = edgesRef.current && (owner?.visible ?? true);
    }
  };

  const applyTransparency = () => {
    sceneRef.current?.children.forEach((child) => {
      if (!(child instanceof THREE.Mesh) || !child.userData.vfMesh) return;
      const material = child.material as THREE.MeshStandardMaterial;
      const baseAlpha = child.userData.baseAlpha ?? 1;
      const translucent = transparentRef.current || baseAlpha < 1;
      material.transparent = translucent;
      material.opacity = cadOpacityFor(baseAlpha, transparentRef.current);
      const shader = material as unknown as THREE.ShaderMaterial;
      if (shader.uniforms?.alpha) shader.uniforms.alpha.value = cadOpacityFor(baseAlpha, transparentRef.current);
      material.depthWrite = !translucent;
      material.needsUpdate = true;
    });
  };

  const setEdgesPreference = (enabled: boolean) => {
    edgesRef.current = enabled;
    setEdgesOn(enabled);
    applyEdges();
  };

  const setTransparentPreference = (enabled: boolean) => {
    transparentRef.current = enabled;
    setTransparentOn(enabled);
    applyTransparency();
  };

  const updateCadGrid = () => {
    const grids = gridsRef.current;
    if (!grids) return;
    for (const plane of CAD_GRID_PLANES) {
      const grid = grids[plane];
      grid.visible = gridPlanesRef.current[plane];
      if (gridCenteredRef.current) grid.position.copy(sceneCenterRef.current);
      else grid.position.set(0, 0, 0);
    }
  };

  const toggleCadGrid = (plane: CadGridPlane, enabled: boolean) => {
    const next = {...gridPlanesRef.current, [plane]: enabled};
    gridPlanesRef.current = next;
    setGridPlanes(next);
    updateCadGrid();
  };

  const applyTheme = (value: unknown) => {
    const normalized = normalizeCadTheme(value);
    themeRef.current = normalized;
    setTheme(normalized);
    document.documentElement.dataset.vfTheme = normalized;
    document.documentElement.setAttribute("data-theme", normalized);
    if (document.body) {
      document.body.dataset.vfTheme = normalized;
      document.body.setAttribute("data-theme", normalized);
    }
    const scene = sceneRef.current;
    if (!scene) return;
    if (gridsRef.current) disposeCadGrids(gridsRef.current);
    const grids = makeCadGrids(normalized === "dark");
    gridsRef.current = grids;
    scene.add(grids.xz, grids.xy, grids.yz);
    updateCadGrid();
    repaintStudio();
    scene.children.forEach((child) => {
      if (child instanceof THREE.LineSegments && child.userData.vfMesh) {
        (child.material as THREE.LineBasicMaterial).color.set(normalized === "dark" ? 0x707070 : 0x000000);
      }
    });
  };

  useEffect(() => { applyTheme(appTheme); }, [appTheme]);

  const rememberTree = (patch: Partial<CadTreePreferences>) => {
    const memory = treeMemory.current;
    if (!memory) return;
    const selection = captureProjectCase();
    if (`${selection?.userId}:${selection?.caseId}` !== memory.owner) return;
    memory.prefs = {...memory.prefs, ...patch};
    if (selection && memory.source !== "local") saveCadTree(selection.userId, selection.caseId, memory.source, memory.prefs);
  };

  // 先完整构建新对象，再替换旧网格；调用方可以安全保留上一有效场景。
  const applySceneDocument = (document: CadSceneDocument, source: CadViewSource = "local") => {
    const scene = sceneRef.current;
    if (!scene) return;
    const selection = captureProjectCase();
    const owner = `${selection?.userId}:${selection?.caseId}`;
    const previous = treeMemory.current;
    const prefs = previous?.owner === owner && previous.source === source ? previous.prefs
      : selection && source !== "local" ? readCadTree(selection.userId, selection.caseId, source) : emptyTree;
    const restored = document.objects.map((item): CadViewedObject => {
      const key = item.treeId ?? item.name;
      return {key, name: item.name, visible: !prefs.hidden.includes(key), bbox: item.bbox,
        vertices: item.vertices.length, faces: item.faces.length, groups: item.groups, hasUV: !!item.uvs};
    });
    const selected = restored.find((item) => item.key === prefs.selected);
    const groupIds = new Set(restored.flatMap((item) => (item.groups ?? []).map((group) => group.id)));
    const collapsed = prefs.collapsed.filter((id) => groupIds.has(id));
    const nextChildren: Array<THREE.Mesh | THREE.LineSegments> = [];
    const allBox = new THREE.Box3();
    for (const [index, o] of document.objects.entries()) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(o.vertices.flat()), 3));
      if (o.uvs) geo.setAttribute("uv", new THREE.Float32BufferAttribute(o.uvs, 2));
      geo.setIndex(new THREE.BufferAttribute(new Uint32Array(o.faces.flat()), 1));
      if (o.normals) geo.setAttribute("normal", new THREE.Float32BufferAttribute(o.normals.flat(), 3));
      else geo.computeVertexNormals();
      const mat = new THREE.MeshPhysicalMaterial({
        color: new THREE.Color(o.color), transparent: transparentRef.current || o.alpha < 1,
        opacity: cadOpacityFor(o.alpha, transparentRef.current), side: THREE.DoubleSide,
        depthWrite: !(transparentRef.current || o.alpha < 1),
        metalness: materialRef.current.metalness, roughness: materialRef.current.roughness,
        emissive: restored[index] === selected ? 0x224466 : 0x000000,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.userData.vfMesh = o.name;
      mesh.userData.vfObjectKey = restored[index].key;
      mesh.name = o.name;
      mesh.userData.home = mesh.position.clone();
      mesh.userData.baseRotation = mesh.rotation.clone();
      mesh.userData.center = new THREE.Vector3(
        (o.bbox.min[0] + o.bbox.max[0]) / 2,
        (o.bbox.min[1] + o.bbox.max[1]) / 2,
        (o.bbox.min[2] + o.bbox.max[2]) / 2,
      );
      mesh.userData.bbox = o.bbox;
      mesh.userData.baseColor = new THREE.Color(o.color);
      mesh.userData.baseAlpha = o.alpha;
      mesh.userData.originalMaterial = mat;
      mesh.userData.sourceGeometry = geo;
      const visible = restored[index].visible;
      mesh.visible = visible;
      const edges = o.edges
        ? new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(o.edges.flat(), 3))
        : new THREE.EdgesGeometry(geo, 30);
      const line = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({
        color: themeRef.current === "dark" ? 0x707070 : 0x000000, transparent: true, opacity: 0.25,
      }));
      line.userData.vfMesh = o.name;
      line.userData.vfObjectKey = restored[index].key;
      line.userData.sourceGeometry = edges;
      line.userData.home = line.position.clone();
      line.userData.baseRotation = line.rotation.clone();
      line.userData.center = mesh.userData.center.clone();
      line.visible = visible && edgesRef.current;
      nextChildren.push(mesh, line);
      allBox.expandByObject(mesh);
    }
    const toRemove = scene.children.filter((child) => child.userData.vfMesh);
    for (const child of toRemove) {
      scene.remove(child);
      zebraMaterialsRef.current.delete(child.uuid);
      disposeCadChild(child);
    }
    zebraMaterialsRef.current.clear();
    nextChildren.forEach((child) => scene.add(child));
    if (trackBindingRef.current) {
      clearTrackBinding();
      setTrackStatus("场景已更新，请重新应用轨道。");
    }
    setObjects(restored);
    setSelectedName(selected?.name ?? "");
    setCollapsedGroups(collapsed);
    treeMemory.current = {source, owner, prefs, selection};
    rememberTree({hidden: restored.filter((item) => !item.visible).map((item) => item.key),
      collapsed, selected: selected?.key ?? ""});
    animStateRef.current.on = false;
    setAnimOn(false);
    animTimeRef.current = 0;
    if (!allBox.isEmpty()) {
      sceneCenterRef.current = allBox.getCenter(new THREE.Vector3());
      sceneSizeRef.current = allBox.getSize(new THREE.Vector3());
    } else {
      sceneCenterRef.current.set(0, 0, 0);
      sceneSizeRef.current.set(1, 1, 1);
    }
    updateCadGrid();
    applyExplode();
    applyClip();
    applyZebra();
    applyEdges();
    applyTransparency();
    applyMaterial();
    applyShadows(shadowRef.current);
    setObjInfo(document.objects.map((o) =>
      `${o.name}（${o.vertices.length} 顶点/${o.faces.length} 面）`).join("，"));
    fitScene();
  };
  applySceneRef.current = applySceneDocument;

  const presentCaseGeometry = (preferAssembly: boolean) => {
    const document = caseGeometry.current;
    if (!document?.selection.isCurrent()) return false;
    const useAssembly = preferAssembly && document.assembly !== undefined;
    if (useAssembly) {
      try {
        applySceneRef.current(normalizeCadAssembly(document.assembly), "assembly");
        setAssemblyOpen(true);
        return true;
      } catch {
        setSceneLoadError("装配层级无法读取，已显示平铺对象。");
      }
    }
    applySceneRef.current(document.ordinary, "case");
    setAssemblyOpen(false);
    return !useAssembly;
  };

  // 加载 scene.json → 网格
  const loadScene = async () => {
    const selection = captureProjectCase();
    if (!selection || selection.caseId !== currentCase?.id || !sceneRef.current) return;
    const sequence = ++sceneReadRef.current;
    const current = () => sequence === sceneReadRef.current && selection.isCurrent();
    viewingStep.current = false;
    try {
      const document = await api.get<PreviewGeometryDocument>(`/cases/${currentCase.id}/geometry/scene`);
      if (current()) {
        const ordinary = normalizeCadScene(document);
        const assembly = cadAssemblyPayload(document);
        caseGeometry.current = {selection, ordinary, assembly};
        setHasAssembly(assembly !== undefined);
        setSceneLoadError("");
        presentCaseGeometry(readCaseSelection(selection.userId, selection.caseId).cadAssembly === true);
        const metadata = previewGeometryMetadata(document);
        setPreviewMetadata(metadata);
        if (!ownTolerance.current && metadata.tolerance !== undefined) setToleranceDraft(String(metadata.tolerance));
      }
    } catch (error) {
      if (current()) setSceneLoadError(`场景加载失败，保留上一有效场景。${error instanceof Error ? error.message : String(error)}`);
    }
  };
  loadSceneRef.current = loadScene;

  const applyExplode = () => {
    const children = sceneRef.current?.children ?? [];
    for (const child of children) {
      if (!child.userData.vfMesh || !child.userData.home || !child.userData.center) continue;
      const direction = child.userData.center.clone().sub(sceneCenterRef.current);
      if (direction.lengthSq() > 0) direction.normalize();
      else direction.set(1, 0.5, 0.25).normalize();
      child.position.copy(child.userData.home).addScaledVector(direction, explodeMotionRef.current.current * 50);
    }
  };

  const geometryData = (geometry: THREE.BufferGeometry) => {
    const attribute = geometry.getAttribute("position");
    const vertices = Array.from({length: attribute.count}, (_, index) => [
      attribute.getX(index), attribute.getY(index), attribute.getZ(index),
    ]);
    const indices = geometry.getIndex();
    const faces: number[][] = [];
    const count = indices ? indices.count : attribute.count;
    for (let index = 0; index < count; index += 3) {
      faces.push(indices ? [indices.getX(index), indices.getX(index + 1), indices.getX(index + 2)]
        : [index, index + 1, index + 2]);
    }
    const uvs = geometry.getAttribute("uv");
    return {vertices, faces, uvs: uvs && Array.from(uvs.array)};
  };

  const geometryFromData = (positions: number[][], faces: number[][], uvs?: number[]) => {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(positions.flat()), 3));
    geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(faces.flat()), 1));
    if (uvs) geometry.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
    geometry.computeVertexNormals();
    return geometry;
  };

  const disposeCadChild = (child: THREE.Object3D) => {
    if (!(child instanceof THREE.Mesh) && !(child instanceof THREE.LineSegments)) return;
    child.geometry.dispose();
    const material = child.material;
    if (Array.isArray(material)) material.forEach(item => item.dispose());
    else material.dispose();
    const original = child.userData.originalMaterial as THREE.Material | undefined;
    if (original && original !== child.material) original.dispose();
  };

  const removeClipCaps = () => {
    const scene = sceneRef.current;
    if (scene) {
      for (const child of scene.children.filter(item => item.userData.clipCap)) {
        scene.remove(child);
        zebraMaterialsRef.current.delete(child.uuid);
        disposeCadChild(child);
      }
    }
    setClipCapCount(0);
  };

  const applyClip = () => {
    const renderer = rendererRef.current;
    const scene = sceneRef.current;
    if (!renderer || !scene) return;
    renderer.clippingPlanes = [];
    if (clipHelperRef.current) {
      scene.remove(clipHelperRef.current);
      disposeCadChild(clipHelperRef.current);
      clipHelperRef.current = null;
    }
    removeClipCaps();
    if (!clipAxis) {
      setClipOpenContours(false);
      for (const child of scene.children) {
        if (!(child instanceof THREE.Mesh) && !(child instanceof THREE.LineSegments)) continue;
        if (!child.userData.vfMesh || child.userData.clipCap) continue;
        const source = child.userData.sourceGeometry as THREE.BufferGeometry | undefined;
        if (source && child.geometry !== source) {
          child.geometry.dispose();
          child.geometry = source;
        }
      }
      applyZebra();
      applyStudioTextures();
      applyEdges();
      applyTransparency();
      applyShadows(shadowRef.current);
      return;
    }
    const originals = scene.children.filter(child => child.userData.vfMesh && !child.userData.clipCap);
    if (!originals.length) {
      setClipOpenContours(false);
      return;
    }
    const center = sceneCenterRef.current;
    const size = sceneSizeRef.current;
    const axisIndex = clipAxis === "x" ? 0 : clipAxis === "y" ? 1 : 2;
    const normal = new THREE.Vector3().setComponent(axisIndex, clipSide === "lower" ? -1 : 1);
    const point = center.clone();
    point.setComponent(axisIndex, center.getComponent(axisIndex) + (clipPos - 0.5) * size.getComponent(axisIndex));
    let capCount = 0;
    let hasOpenContours = false;
    for (const child of originals) {
      if (!(child instanceof THREE.Mesh) || !child.userData.sourceGeometry) continue;
      const source = child.userData.sourceGeometry as THREE.BufferGeometry;
      const data = geometryData(source);
      const clipped = clipCadMesh(data.vertices, data.faces,
        {normal: normal.toArray(), point: point.toArray()}, data.uvs);
      hasOpenContours ||= clipped.openContours;
      const nextGeometry = geometryFromData(clipped.positions, clipped.faces, clipped.uvs);
      if (child.geometry !== source) child.geometry.dispose();
      child.geometry = nextGeometry;
      const line = originals.find(item => item instanceof THREE.LineSegments && item.userData.vfMesh === child.userData.vfMesh) as THREE.LineSegments | undefined;
      if (line) {
        const lineSource = line.userData.sourceGeometry as THREE.BufferGeometry | undefined;
        if (lineSource && line.geometry !== lineSource) line.geometry.dispose();
        line.geometry = new THREE.EdgesGeometry(nextGeometry, 30);
      }
      if (clipped.capFaces.length) {
        const capGeometry = geometryFromData(clipped.capPositions, clipped.capFaces);
        const capMaterial = (child.userData.originalMaterial as THREE.MeshStandardMaterial).clone();
        capMaterial.map = null;
        const cap = new THREE.Mesh(capGeometry, capMaterial);
        cap.userData = {...child.userData, clipCap: true, sourceGeometry: capGeometry};
        cap.userData.originalMaterial = cap.material;
        cap.position.copy(child.position);
        cap.rotation.copy(child.rotation);
        cap.visible = child.visible;
        scene.add(cap);
        if (line) {
          const capLine = new THREE.LineSegments(new THREE.EdgesGeometry(capGeometry, 30),
            (line.material as THREE.Material).clone());
          capLine.userData = {...line.userData, clipCap: true, sourceGeometry: capLine.geometry};
          capLine.userData.originalMaterial = capLine.material;
          capLine.position.copy(line.position);
          capLine.rotation.copy(line.rotation);
          capLine.visible = child.visible && edgesRef.current;
          scene.add(capLine);
        }
        capCount += 1;
      }
    }
    setClipCapCount(capCount);
    setClipOpenContours(hasOpenContours);
    const geometry = new THREE.PlaneGeometry(Math.max(size.x, size.y, size.z) * 1.2,
                                             Math.max(size.x, size.y, size.z) * 1.2);
    const material = new THREE.MeshBasicMaterial({ color: 0x2266aa, transparent: true,
                                                   opacity: 0.18, side: THREE.DoubleSide });
    const helper = new THREE.Mesh(geometry, material);
    helper.position.copy(point);
    helper.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal);
    scene.add(helper);
    clipHelperRef.current = helper;
    if (trackPoseRef.current) applyTrackAtTime(trackTimeRef.current);
    else if (!animStateRef.current.on) applyExplode();
    applyZebra();
    applyStudioTextures();
    applyEdges();
    applyTransparency();
    applyShadows(shadowRef.current);
  };

  const applyZebra = () => {
    const textureKey = `${zebraCount}:${zebraScheme}:${zebraScheme === "custom" ? zebraColors.join(",") : ""}`;
    if (!zebraTextureRef.current || zebraTextureKeyRef.current !== textureKey) {
      zebraTextureRef.current?.dispose();
      const texture = new THREE.DataTexture(buildZebraPixels(zebraCount, zebraScheme, zebraColors), 1, ZEBRA_TEXTURE_HEIGHT,
        THREE.RGBAFormat, THREE.UnsignedByteType);
      texture.magFilter = THREE.NearestFilter;
      texture.minFilter = THREE.NearestFilter;
      texture.wrapS = THREE.RepeatWrapping;
      texture.wrapT = THREE.RepeatWrapping;
      texture.generateMipmaps = false;
      texture.needsUpdate = true;
      zebraTextureRef.current = texture;
      zebraTextureKeyRef.current = textureKey;
    }
    const texture = zebraTextureRef.current;
    const direction = zebraDirectionVector(zebraAngle);
    sceneRef.current?.children.forEach((child) => {
      if (!(child instanceof THREE.Mesh) || !child.userData.vfMesh) return;
      if (zebraOn) {
        const original = child.userData.originalMaterial as THREE.MeshStandardMaterial;
        let material = zebraMaterialsRef.current.get(child.uuid);
        if (!material) {
          material = new THREE.ShaderMaterial({
            uniforms: {
              zebraTexture: {value: texture}, direction: {value: new THREE.Vector3()},
              opacity: {value: zebraOpacity}, baseColor: {value: original.color.clone()},
              alpha: {value: original.opacity}, mappingMode: {value: zebraMapping === "reflection" ? 0 : 1},
            },
            vertexShader: ZEBRA_VERTEX_SHADER,
            fragmentShader: ZEBRA_FRAGMENT_SHADER,
            side: THREE.DoubleSide,
          });
          zebraMaterialsRef.current.set(child.uuid, material);
        }
        material.uniforms.zebraTexture.value = texture;
        material.uniforms.direction.value.set(direction[0], direction[1], direction[2]).normalize();
        material.uniforms.opacity.value = zebraOpacity;
        material.uniforms.baseColor.value.copy(original.color);
        material.uniforms.alpha.value = cadOpacityFor(child.userData.baseAlpha ?? 1, transparentRef.current);
        material.uniforms.mappingMode.value = zebraMapping === "reflection" ? 0 : 1;
        child.material = material;
      } else {
        child.material = child.userData.originalMaterial;
      }
    });
  };

  const resetAnimation = () => {
    animTimeRef.current = 0;
    sceneRef.current?.children.forEach((child) => {
      if (!child.userData.vfMesh) return;
      child.position.copy(child.userData.home);
      child.rotation.copy(child.userData.baseRotation);
    });
    applyExplode();
  };

  const changeExplodePhase = (phase: ExplodePhase) => {
    explodeMotionRef.current.phase = phase;
    setExplodePhase(phase);
  };

  const stopExplode = () => {
    setExplodeOn(false);
    Object.assign(explodeMotionRef.current, {current: 0, from: 0, target: 0, elapsed: 0, reported: 0});
    changeExplodePhase("idle");
    applyExplode();
    setExplodeProgress(0);
  };

  const toggleExplode = (enabled: boolean) => {
    if (enabled) {
      if (trackPlayingRef.current || trackPoseRef.current) stopTrack();
      animStateRef.current.on = false;
      setAnimOn(false);
      resetAnimation();
    } else if (explodeMotionRef.current.phase === "paused") {
      changeExplodePhase("idle");
    }
    setExplodeOn(enabled);
  };

  const applyTrackAtTime = (time: number) => {
    const binding = trackBindingRef.current;
    if (!binding || !sceneRef.current) return;
    resetAnimation();
    const targets = new Map<string, THREE.Object3D[]>();
    for (const child of sceneRef.current.children) {
      const key = child.userData.vfObjectKey as string | undefined;
      if (key === undefined) continue;
      const group = targets.get(key);
      if (group) group.push(child);
      else targets.set(key, [child]);
    }
    for (const {key, track} of binding.tracks) {
      const {action} = track;
      const sample = sampleCadAnimationTrack(track, time);
      for (const child of targets.get(key) ?? []) {
        if (action === "t") {
          const [x, y, z] = sample as number[];
          child.position.x += x;
          child.position.y += y;
          child.position.z += z;
        } else if (action === "tx" || action === "ty" || action === "tz") {
          const axis = action === "tx" ? 0 : action === "ty" ? 1 : 2;
          child.position.setComponent(axis, child.position.getComponent(axis) + (sample as number));
        } else if (action === "q") {
          const [x, y, z, w] = sample as number[];
          child.quaternion.multiply(new THREE.Quaternion(x, y, z, w));
        } else {
          const axis = action === "rx" ? new THREE.Vector3(1, 0, 0)
            : action === "ry" ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1);
          child.quaternion.multiply(new THREE.Quaternion().setFromAxisAngle(axis,
            THREE.MathUtils.degToRad(sample as number)));
        }
      }
    }
    trackPoseRef.current = true;
    trackTimeRef.current = time;
    setTrackTime(time);
  };

  const applyTrackDocument = () => {
    try {
      const document = normalizeCadAnimation(trackText);
      const tracks = bindCadAnimationTracks(document, objects);
      trackBindingRef.current = {document, tracks};
      setTrackDoc(document);
      stopExplode();
      animStateRef.current.on = false;
      setAnimOn(false);
      trackPlayingRef.current = false;
      setTrackPlaying(false);
      applyTrackAtTime(0);
      setTrackStatus(`已应用 ${document.tracks.length} 条轨道，时长 ${document.duration.toFixed(3)}s`);
    } catch (error) {
      setTrackStatus(`轨道错误：${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const clearTrackBinding = () => {
    trackBindingRef.current = null;
    trackPoseRef.current = false;
    trackPlayingRef.current = false;
    trackTimeRef.current = 0;
    setTrackDoc(null);
    setTrackPlaying(false);
    setTrackTime(0);
  };

  const addTrackForObject = (object: CadObjectState) => {
    try {
      const starting = trackText === DEFAULT_TRACK_TEXT && !trackBindingRef.current;
      const document = starting ? {schema: CAD_ANIMATION_SCHEMA, speed: 1, repeat: false, tracks: []}
        : normalizeCadAnimation(trackText);
      const zero = trackAction === "t" ? [0, 0, 0] : trackAction === "q" ? [0, 0, 0, 1] : 0;
      const next = {
        ...document, tracks: [...document.tracks, {selector: object.name.slice(0, 120), objectId: object.key,
          action: trackAction, times: [0, 1], values: [zero, zero]}],
      };
      setTrackText(canonicalCadAnimationJson(next));
      setTrackStatus(`已为 ${object.name} 添加轨道。编辑关键帧后应用。`);
      trackInputRef.current?.focus();
    } catch (error) {
      setTrackStatus(`轨道错误：${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const playTrack = () => {
    const document = trackBindingRef.current?.document;
    if (!document) return;
    stopExplode();
    animStateRef.current.on = false;
    setAnimOn(false);
    if (trackTimeRef.current >= document.duration) trackTimeRef.current = 0;
    applyTrackAtTime(trackTimeRef.current);
    trackPlayingRef.current = true;
    setTrackPlaying(true);
    setTrackStatus("轨道播放中");
  };

  const pauseTrack = () => {
    trackPlayingRef.current = false;
    setTrackPlaying(false);
    setTrackStatus("轨道已暂停");
  };

  const stopTrack = () => {
    trackPlayingRef.current = false;
    trackPoseRef.current = false;
    setTrackPlaying(false);
    resetAnimation();
    trackTimeRef.current = 0;
    setTrackTime(0);
    setTrackStatus("轨道已停止并恢复源位置");
  };

  const exportTrack = () => {
    try {
      const blob = new Blob([canonicalCadAnimationJson(trackBindingRef.current?.document ?? trackText)], {type: "application/json"});
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "cad-animation.json";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setTrackStatus("轨道 JSON 已导出");
    } catch (error) {
      setTrackStatus(`轨道导出失败：${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const toggleAnimation = () => {
    const enabled = !animOn;
    if (enabled) {
      if (trackPlayingRef.current || trackPoseRef.current) stopTrack();
      stopExplode();
      animTimeRef.current = 0;
    } else {
      resetAnimation();
    }
    animStateRef.current.on = enabled;
    setAnimOn(enabled);
  };

  const cameraPreset = (preset: CadViewPreset) => {
    const center = sceneCenterRef.current;
    const size = sceneSizeRef.current;
    if (viewModeRef.current === "quad") {
      quadViewsRef.current[activeQuadRef.current]?.fit(center, size, preset);
      refreshQuadSelection(activeQuadRef.current);
      return;
    }
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls) return;
    const distance = Math.max(size.length() * 0.9, 30);
    const direction = new THREE.Vector3(...CAD_VIEW_DIRECTIONS[preset]);
    camera.position.copy(center).addScaledVector(direction, distance);
    const up = new THREE.Vector3(0, preset === "y" ? 0 : 1, preset === "y" ? 1 : 0);
    const differentUp = !camera.up.equals(up);
    camera.up.copy(up);
    camera.zoom = 1;
    camera.far = Math.max(50, distance * 4);
    if (camera instanceof THREE.OrthographicCamera) {
      updateOrthographicProjection(camera, cadOrthoSpan(sceneSizeRef.current.toArray(), viewportAspectRef.current));
    } else {
      camera.updateProjectionMatrix();
    }
    controls.target.copy(center);
    if (differentUp) recreateControls(camera);
    else controls.update();
  };

  const centerOf = (object: CadObjectState) => object.bbox.min.map((value, index) =>
    (value + object.bbox.max[index]) / 2);
  const formatNumber = (value: number) => String(Math.abs(value) < 1e-9 ? 0 : Number(value.toFixed(3)));
  const selectedObject = objects.find((object) => object.name === selectedName);
  const selectedTrackActions = trackBindingRef.current?.tracks
    .filter(item => item.key === selectedObject?.key).map(item => CAD_TRACK_ACTION_LABELS[item.track.action]) ?? [];
  const compareObject = objects.find((object) => object.name === compareName);
  const centerDistance = selectedObject && compareObject && selectedObject.name !== compareObject.name
    ? Math.sqrt(centerOf(selectedObject).reduce((sum, value, index) =>
        sum + (value - centerOf(compareObject)[index]) ** 2, 0))
    : null;
  useEffect(() => {
    setCompareName((name) => objects.some((object) => object.name === name) ? name : "");
  }, [objects]);

  const toggleObjects = (names: string[]) => {
    const targets = new Set(names);
    const visible = objects.some((item) => targets.has(item.name) && !item.visible);
    const next = objects.map((item) => targets.has(item.name) ? {...item, visible} : item);
    setObjects(next);
    rememberTree({hidden: next.filter((item) => !item.visible).map((item) => item.key)});
    sceneRef.current?.children.forEach((child) => {
      if (!targets.has(child.userData.vfMesh)) return;
      child.visible = child instanceof THREE.LineSegments ? visible && edgesRef.current : visible;
    });
  };

  const selectObject = (name: string) => {
    setSelectedName(name);
    rememberTree({selected: objects.find((item) => item.name === name)?.key ?? ""});
    sceneRef.current?.children.forEach((child) => {
      if (child instanceof THREE.Mesh && child.userData.vfMesh) {
        const material = child.material as THREE.MeshStandardMaterial;
        if (material.emissive) material.emissive.set(child.userData.vfMesh === name ? 0x224466 : 0x000000);
      }
    });
  };

  const toggleGroup = (id: string) => {
    const next = collapsedGroups.includes(id) ? collapsedGroups.filter((group) => group !== id) : [...collapsedGroups, id];
    setCollapsedGroups(next);
    rememberTree({collapsed: next});
  };

  const toggleTree = () => {
    const open = !treeOpen;
    setTreeOpen(open);
    const selection = captureProjectCase();
    if (selection) saveCaseSelection(selection.userId, selection.caseId, {cadTreeOpen: open});
  };

  const chooseAssembly = (assembly: boolean) => {
    const selection = caseGeometry.current?.selection;
    if (!selection?.isCurrent()) return;
    setSceneLoadError("");
    if (presentCaseGeometry(assembly)) saveCaseSelection(selection.userId, selection.caseId, {cadAssembly: assembly});
  };

  const captureImage = (): string | null => {
    const renderer = rendererRef.current;
    const drawn = drawnScene.current;
    if (!renderer || !drawn?.selection?.isCurrent() || drawn !== treeMemory.current ||
        !sceneRef.current?.children.some(child => child instanceof THREE.Mesh && child.userData.vfMesh)) return null;
    const canvas = renderer.domElement;
    if (!canvas.width || !canvas.height || renderer.getContext().isContextLost()) return null;
    try {
      const data = canvas.toDataURL("image/png");
      return data.startsWith("data:image/png;base64,") ? data : null;
    } catch {
      return null;
    }
  };

  const screenshot = () => {
    const data = captureImage();
    if (!data) {
      setStatus("未能截取 CAD 视图。请等待当前模型显示后重试。");
      return;
    }
    const anchor = document.createElement("a");
    anchor.href = data;
    anchor.download = `${currentCase?.name ?? "geometry"}-cad.png`;
    anchor.click();
    setStatus("已截取 CAD 视图");
  };

  const clearWatch = (watch: CadJobWatch) => {
    watch.generation += 1;
    if (watch.timer !== null) {
      clearInterval(watch.timer);
      watch.timer = null;
    }
  };

  const watchJob = (jobId: number, mode: "preview" | "export" = "preview", selection = captureProjectCase()) => {
    if (!selection?.isCurrent()) return;
    const exporting = mode === "export";
    const watch = (exporting ? exportWatch : previewWatch).current;
    clearWatch(watch);
    const generation = watch.generation;
    const current = () => generation === watch.generation && selection.isCurrent();
    const reportFinished = (message: string) => {
      if (!current()) return;
      if (exporting) {
        exportSelection.current = null;
        setExportBusy(false);
        setExportStatus(message);
      } else setStatus(message);
    };
    const finish = () => {
      clearInterval(timer);
      if (watch.timer === timer) watch.timer = null;
    };
    let polls = 0;
    let reading = false;
    const timer = window.setInterval(async () => {
      if (reading || !current()) return;
      reading = true;
      polls += 1;
      try {
        const st = await api.get<Job>(`/jobs/${jobId}`);
        if (!current()) return;
        if (st.status === "succeeded") {
          finish();
          if (exporting) {
            const published = `STEP/STL 导出完成（作业 #${jobId}）`;
            try {
              await useStore.getState().loadTree();
              reportFinished(published + "。可查看 A-geo/exports 或读取最新发布。");
            } catch {
              reportFinished(published + "。文件树暂时无法刷新，可在已发布的 STEP / STL 中读取最新发布。");
            }
          } else {
            setStatus("完成，加载场景…");
            await loadSceneRef.current();
            if (current()) setStatus("");
          }
        } else if (["failed", "cancelled", "interrupted"].includes(st.status)) {
          finish();
          reportFinished(exporting ? `STEP/STL 导出失败（作业 #${jobId}）。已有发布仍可下载。`
            : `预览失败（作业 #${jobId}），保留上一有效场景`);
        } else if (polls >= (exporting ? 300 : 240)) {
          finish();
          reportFinished(exporting ? `未能确认导出作业 #${jobId} 已结束，请在作业面板核对。`
            : "预览超时，保留上一有效场景");
        }
      } catch {
        if (!current()) return;
        finish();
        reportFinished(exporting ? `导出作业 #${jobId} 状态读取失败，请在作业面板核对。`
          : "作业状态读取失败，保留上一有效场景");
      } finally {
        reading = false;
      }
    }, 1000);
    watch.timer = timer;
  };

  const rebuild = async () => {
    const selection = captureProjectCase();
    if (!selection || invalidTolerance) return;
    try {
      setStatus("提交预览作业…");
      const j = await api.post<{ job_id: number }>(`/cases/${selection.caseId}/geometry:preview`, {
        tolerance: chosenTolerance,
      });
      if (!selection.isCurrent()) return;
      if (autoPreviewError) useStore.getState().clearAutoPreviewError(selection.caseId, autoPreviewError);
      ownTolerance.current = true;
      saveCaseSelection(selection.userId, selection.caseId, {previewTolerance: chosenTolerance});
      setStatus(`作业 #${j.job_id} 运行中…`);
      watchJob(j.job_id);
    } catch (error) {
      if (selection.isCurrent()) setStatus(error instanceof Error ? error.message : String(error));
    }
  };

  const exportGeometry = async () => {
    const selection = captureProjectCase();
    if (!selection || exportSelection.current?.isCurrent()) return;
    exportSelection.current = selection;
    setExportBusy(true);
    try {
      setExportStatus("提交 STEP/STL 导出作业…");
      const job = await api.post<{ job_id: number }>(`/cases/${selection.caseId}/geometry:export`, {});
      if (!selection.isCurrent()) return;
      setExportStatus(`STEP/STL 导出作业 #${job.job_id} 运行中…`);
      watchJob(job.job_id, "export", selection);
    } catch (error) {
      if (!selection.isCurrent()) return;
      exportSelection.current = null;
      setExportBusy(false);
      setExportStatus(`无法提交 STEP/STL 导出。${error instanceof Error ? error.message : String(error)}`);
    }
  };

  useEffect(() => {
    const loadBase64 = (encoded: string) => {
      try {
        const document = decodeCadSceneBase64(encoded);
        ++sceneReadRef.current;
        applySceneRef.current(document);
        caseGeometry.current = null;
        setHasAssembly(false);
        setAssemblyOpen(false);
        setPreviewMetadata(undefined);
        setStatus(`Base64 场景已加载（${document.objects.length} 个对象，仅当前视图）`);
      } catch (error) {
        setStatus(`Base64 场景加载失败，保留上一有效场景：${error instanceof Error ? error.message : String(error)}`);
      }
    };
    const fit = () => fitScene();
    const edges = (enabled: boolean) => setEdgesPreference(!!enabled);
    const transparent = (enabled: boolean) => setTransparentPreference(!!enabled);
    const changeTheme = (themeId: string) => applyTheme(themeId);
    const onScreenshot = (event: MessageEvent) => {
      const samePage = event.source === window && event.origin === location.origin;
      const localEvent = event.source === null && event.origin === "";
      if (!samePage && !localEvent) return;
      let request = event.data;
      if (typeof request === "string") {
        try { request = JSON.parse(request); }
        catch { return; }
      }
      if (request?.type !== "screenshot" || typeof request.filename !== "string") return;
      window.postMessage({command: "screenshot", text: {filename: request.filename, data: captureImage()}},
        location.origin);
    };
    window.vfLoadSceneFromBase64 = loadBase64;
    window.vfFit = fit;
    window.vfSetEdges = edges;
    window.vfSetTransparent = transparent;
    window.setVibeFlowTheme = changeTheme;
    window.addEventListener("message", onScreenshot);
    return () => {
      window.removeEventListener("message", onScreenshot);
      if (window.vfLoadSceneFromBase64 === loadBase64) delete window.vfLoadSceneFromBase64;
      if (window.vfFit === fit) delete window.vfFit;
      if (window.vfSetEdges === edges) delete window.vfSetEdges;
      if (window.vfSetTransparent === transparent) delete window.vfSetTransparent;
      if (window.setVibeFlowTheme === changeTheme) delete window.setVibeFlowTheme;
    };
  }, []);

  useEffect(() => {
    if (!currentCase) return;
    const selection = captureProjectCase();
    let firstNotice = true;
    return followCadScenes(currentCase.id, notice => {
      if (!selection?.isCurrent()) return;
      const startingFromCase = firstNotice && notice.type === "scene.snapshot" && notice.source === "case";
      firstNotice = false;
      if (!startingFromCase && !viewingStep.current) void loadSceneRef.current();
    }, detail => {
      if (selection?.isCurrent()) setSceneLoadError(`场景同步失败：${detail}`);
    });
  }, [currentCase?.id, user?.id]);

  useEffect(() => {
    ++sceneReadRef.current;
    viewingStep.current = false;
    treeMemory.current = null;
    caseGeometry.current = null;
    setHasAssembly(false);
    setAssemblyOpen(false);
    stopExplode();
    clearTrackBinding();
    setTrackText(DEFAULT_TRACK_TEXT);
    setTrackStatus("");
    surfaceOverrides.current.clear();
    ++studioImageRequest.current;
    replaceStudioImage(null);
    applySceneRef.current({ objects: [] });
    setObjInfo("");
    setStatus("");
    setSceneLoadError("");
    const preferences = user && currentCase ? readCaseSelection(user.id, currentCase.id) : undefined;
    setTreeOpen(preferences?.cadTreeOpen ?? drawerDefault());
    const preference = preferences?.previewTolerance;
    ownTolerance.current = preference !== undefined;
    setToleranceDraft(String(preference ?? DEFAULT_TOLERANCE));
    setPreviewMetadata(undefined);
    clearWatch(previewWatch.current);
    clearWatch(exportWatch.current);
    exportSelection.current = null;
    setExportBusy(false);
    setExportStatus("");
    void loadSceneRef.current();
    return () => {
      ++sceneReadRef.current;
      clearWatch(previewWatch.current);
      clearWatch(exportWatch.current);
      exportSelection.current = null;
    };
  }, [currentCase?.id, user?.id]);

  useEffect(() => {
    if (!autoPreviewJobId) return;
    setStatus(`预览作业 #${autoPreviewJobId} 运行中…`);
    watchJob(autoPreviewJobId);
  }, [autoPreviewJobId, currentCase?.id, user?.id]);

  useEffect(() => {
    if (filePreview?.kind !== "step" || useStore.getState().filePreviewRequest !== filePreview) return;
    useStore.setState({ filePreviewRequest: null });
    const { intent, path } = filePreview;
    if (!intent.isCurrent()) return;
    const sequence = ++sceneReadRef.current;
    const current = () => sequence === sceneReadRef.current && intent.isCurrent();
    viewingStep.current = true;
    setStatus(`正在导入 STEP ${path}…`);
    void api.get<CadSceneDocument>(`/cases/${intent.caseId}/geometry/import-scene?path=${encodeURIComponent(path)}`)
      .then(document => {
        if (!current()) return;
        applySceneRef.current(normalizeCadScene(document));
        caseGeometry.current = null;
        setHasAssembly(false);
        setAssemblyOpen(false);
        setPreviewMetadata(undefined);
        setStatus(`STEP 已载入（只读） ${path}`);
      })
      .catch(error => {
        if (current()) setStatus(`STEP 导入失败。${error instanceof Error ? error.message : String(error)}`);
      })
      .finally(() => {
        if (sequence === sceneReadRef.current && !intent.isCurrent()) setStatus("");
      });
  }, [filePreview]);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const readPreference = () => setReduceExplodeMotion(query.matches);
    query.addEventListener("change", readPreference);
    return () => query.removeEventListener("change", readPreference);
  }, []);

  useEffect(() => {
    const target = explodeOn ? explodeAmount : 0;
    const motion = explodeMotionRef.current;
    if (!explodeAnimated || reduceExplodeMotion) {
      if (motion.current === target && motion.target === target && motion.phase === "idle") return;
      Object.assign(motion, {current: target, from: target, target, elapsed: 0, reported: target});
      changeExplodePhase("idle");
      applyExplode();
      setExplodeProgress(target);
    } else if (motion.target !== target || motion.duration !== explodeDuration || motion.easing !== explodeEasing) {
      Object.assign(motion, {from: motion.current, target, elapsed: 0, duration: explodeDuration, easing: explodeEasing});
      if (motion.current === target) changeExplodePhase("idle");
      else if (motion.phase !== "paused") changeExplodePhase("playing");
    }
  }, [explodeOn, explodeAmount, explodeAnimated, explodeDuration, explodeEasing, reduceExplodeMotion]);
  useEffect(() => { applyClip(); }, [clipAxis, clipPos, clipSide]);
  useEffect(() => {
    if (viewMode !== "quad") return;
    ensureQuadViews();
    quadViewsRef.current.forEach((view, index) => {
      const element = quadElementsRef.current[index];
      if (element) view.attach(element);
    });
    return () => quadViewsRef.current.forEach(view => view.detach());
  }, [viewMode]);
  useEffect(() => { applyZebra(); }, [zebraOn, zebraCount, zebraOpacity, zebraScheme, zebraColors, zebraMapping, zebraAngle]);
  useEffect(() => { setCompareName(""); }, [selectedName]);
  interface Job { status: string }
  const visibleSurface = selectedSurface();
  const visibleSurfacePreset = Object.entries(CAD_SURFACES).find(([, preset]) =>
    (Object.keys(CAD_SURFACE_FIELDS) as (keyof CadSurfaceSettings)[]).every(key => preset.values[key] === visibleSurface[key]))?.[0] ?? "custom";
  const visibleProjection = viewMode === "quad" ? quadControls.projection : orthoOn;
  const visibleControl = viewMode === "quad" ? quadControls.mode : controlMode;

  // 挂载容器始终渲染（dockview 面板无算例时也会挂载），hint 用覆盖层
  return (
    <div className="geo-panel">
      <div className="geo-toolbar" data-auto-job={autoPreviewJobId ?? ""}>
        <label className="geo-preview-tolerance">预览容差
          <input type="number" step="any" inputMode="decimal" value={toleranceDraft} disabled={!currentCase}
            aria-invalid={invalidTolerance} aria-describedby={invalidTolerance ? "geo-tolerance-error" : undefined}
            onChange={event => { ownTolerance.current = true; setToleranceDraft(event.target.value); }} />
        </label>
        <button style={s.btn} onClick={rebuild} disabled={!currentCase || invalidTolerance}
          title="执行 A-geo/geometry.py 重建预览">重建预览</button>
        <button style={s.miniBtn} onClick={loadScene} disabled={!currentCase}>重载场景</button>
        <button style={s.miniBtn} onClick={exportGeometry} disabled={!currentCase || exportBusy}>导出 STEP/STL</button>
        <button style={s.miniBtn} aria-expanded={treeOpen} aria-controls="geometry-object-settings" onClick={toggleTree}>{treeOpen ? '收起对象与设置' : '对象与设置'}</button>
        {invalidTolerance && <span className="geo-status" id="geo-tolerance-error">请输入大于零的有限数值。</span>}
        {previewMetadata?.tolerance !== undefined && <span className="geo-preview-result" data-testid="preview-tolerance-applied">
          已应用容差 {previewMetadata.tolerance}
          {previewMetadata.ocpLabel && <> · {previewMetadata.ocpLabel}</>}
        </span>}
        {autoPreviewError && <span className="geo-status" role="status" data-testid="geo-auto-preview-warning">
          几何脚本已保存，自动预览未启动。{autoPreviewError.message}
          {!viewingStep.current && objects.length > 0 && " 当前显示上一场景。"}
          {" 可手动重建预览。"}
        </span>}
        <details className="geo-view-options"><summary>视角与截图</summary><div className="geo-view-actions">
        <button style={s.miniBtn} onClick={screenshot} disabled={!currentCase}>截图</button>
        <button data-testid="cad-camera-x" style={s.miniBtn} onClick={() => cameraPreset("x")}>+X</button>
        <button data-testid="cad-camera-y" style={s.miniBtn} onClick={() => cameraPreset("y")}>+Y</button>
        <button data-testid="cad-camera-z" style={s.miniBtn} onClick={() => cameraPreset("z")}>+Z</button>
        <button data-testid="cad-camera-iso" style={s.miniBtn} onClick={() => cameraPreset("iso")}>等轴测</button>
        <button data-testid="cad-fit" style={s.miniBtn} onClick={() => fitScene(true)} disabled={!objects.length}>Fit</button>
        </div></details>
        <span className="geo-status" data-testid="geo-export-status" role="status">{exportStatus}</span>
        {user && currentCase && <GeometryDownloads key={`${user.id}:${currentCase.id}`} userId={user.id} caseId={currentCase.id} />}
        {user && currentCase && <GeometryValidation key={`validation:${user.id}:${currentCase.id}`}
          userId={user.id} caseId={currentCase.id} />}
        <span className="geo-status" data-testid="geo-status" role="status">{status || sceneLoadError}</span>
        <span className="geo-object-info" data-testid="geo-obj-info">{objInfo}</span>
      </div>
      <div className="geo-content">
        {treeOpen && (
          <div className="geo-tree" id="geometry-object-settings" data-testid="cad-tree" aria-label="几何对象与设置">
            <div className="geo-tree-view">
              <span>CAD 对象</span>
              {hasAssembly && <select aria-label="对象树视图" value={assemblyOpen ? "assembly" : "flat"}
                onChange={(e) => chooseAssembly(e.target.value === "assembly")}>
                <option value="flat">平铺</option>
                <option value="assembly">装配</option>
              </select>}
            </div>
            <CadObjectTree objects={objects} selected={selectedName} collapsed={collapsedGroups}
              onSelect={selectObject} onToggleVisibility={toggleObjects} onToggleGroup={toggleGroup} />
            {selectedObject && (
              <div style={s.section} data-testid="cad-properties">
                <strong>{selectedObject.name}</strong>
                <div>{selectedObject.vertices} 顶点 / {selectedObject.faces} 面</div>
                <div>包围盒 {selectedObject.bbox.min.map(formatNumber).join(", ")} ~ {selectedObject.bbox.max.map(formatNumber).join(", ")}</div>
                <div>尺寸 {selectedObject.bbox.max.map((value, index) => formatNumber(value - selectedObject.bbox.min[index])).join(" × ")}</div>
                <div>中心 {centerOf(selectedObject).map(formatNumber).join(", ")}</div>
                <label>对比对象
                  <select data-testid="cad-compare" value={compareName} disabled={objects.length < 2}
                          onChange={(e) => setCompareName(e.target.value)}>
                    <option value="">选择对比对象</option>
                    {objects.filter((object) => object.name !== selectedName).map((object) => (
                      <option key={object.name} value={object.name}>{object.name}</option>
                    ))}
                  </select>
                </label>
                {centerDistance !== null && <div role="status" data-testid="cad-distance">
                  包围盒中心距离 {centerDistance.toFixed(3)}
                </div>}
                {selectedTrackActions.length > 0 && <p className="geo-motion-note" data-testid="cad-object-tracks">
                  已应用轨道 {selectedTrackActions.join("、")}
                </p>}
                <label className="geo-motion-setting">轨道动作
                  <select value={trackAction} onChange={(event) => setTrackAction(event.target.value as CadAnimationAction)}>
                    {CAD_ANIMATION_ACTIONS.map(action => <option value={action} key={action}>
                      {CAD_TRACK_ACTION_LABELS[action]}
                    </option>)}
                  </select>
                </label>
                <button type="button" style={s.miniBtn} onClick={() => addTrackForObject(selectedObject)}>添加对象轨道</button>
              </div>
            )}
            <div style={s.section}>
              <label><input data-testid="cad-edges" type="checkbox" checked={edgesOn}
                            onChange={(e) => setEdgesPreference(e.target.checked)} /> 边线</label>
              <label><input data-testid="cad-transparent" type="checkbox" checked={transparentOn}
                            onChange={(e) => setTransparentPreference(e.target.checked)} /> 透明</label>
              <label>主题
                <select data-testid="cad-theme" value={theme} onChange={(e) => applyTheme(e.target.value)}>
                  <option value="dark">深色</option><option value="light">浅色</option>
                </select>
              </label>
            </div>
            <div style={s.section}>
              <label><input data-testid="cad-ortho" type="checkbox" checked={visibleProjection}
                            onChange={(e) => setOrthoPreference(e.target.checked)} /> 正交</label>
              <label>控制
                <select data-testid="cad-control" value={visibleControl} onChange={(e) => setControlPreference(e.target.value)}>
                  <option value="orbit">Orbit</option><option value="trackball">Trackball</option>
                </select>
              </label>
              <label>视图
                <select data-testid="cad-view-mode" value={viewMode} onChange={(e) => setViewModePreference(e.target.value)}>
                  <option value="single">单视图</option><option value="quad">四宫格</option>
                </select>
              </label>
              <div className="geo-grid-toggles" role="group" aria-label="坐标网格">
                {CAD_GRID_PLANES.map(plane => <label key={plane} className="geo-cad-toggle">
                  <input type="checkbox" data-testid={`cad-grid-${plane}`} checked={gridPlanes[plane]}
                         onChange={e => toggleCadGrid(plane, e.target.checked)} />{plane.toUpperCase()} 网格
                </label>)}
                <label className="geo-cad-toggle"><input type="checkbox" data-testid="cad-grid-center" checked={gridCentered}
                  onChange={e => {
                    gridCenteredRef.current = e.target.checked;
                    setGridCentered(e.target.checked);
                    updateCadGrid();
                  }} />网格居中</label>
              </div>
              <label><input data-testid="cad-studio" type="checkbox" checked={studioOn}
                            onChange={(e) => applyStudio(e.target.checked)} /> Studio 环境</label>
              {studioOn && <div className="geo-render-settings">
                <CadStudioLightingControls settings={studioLighting} size={environmentSize} status={environmentStatus}
                  shadows={shadowOn} onChange={changeStudioLighting} />
                <label>纹理映射
                  <select data-testid="cad-studio-mapping" value={studioMappingMode}
                          onChange={(e) => updateStudioMapping(e.target.value)}>
                    <option value="triplanar">三平面投影</option>
                    <option value="parametric">参数化 UV</option>
                  </select>
                </label>
                {studioMappingMode === "parametric" && studioImageName && objects.some(object => !object.hasUV) &&
                  <p className="geo-render-feedback">没有 UV 的对象使用三平面投影。</p>}
                <label>选择纹理图片
                  <input data-testid="cad-studio-image" type="file"
                         accept="image/png,image/jpeg,image/webp" disabled={!currentCase || !objects.length}
                         onChange={e => {
                           const file = e.currentTarget.files?.[0];
                           e.currentTarget.value = "";
                           if (file) void loadStudioImage(file);
                         }} />
                </label>
                {studioImageName && <div className="geo-render-file">
                  <span>{studioImageName}</span>
                  <button style={s.miniBtn} onClick={() => {
                    ++studioImageRequest.current;
                    replaceStudioImage(null);
                  }}>移除纹理</button>
                </div>}
                {studioImageStatus && <p className="geo-render-feedback" role="status">{studioImageStatus}</p>}
              </div>}
              <label><input data-testid="cad-shadow" type="checkbox" checked={shadowOn}
                            onChange={(e) => applyShadows(e.target.checked)} /> 阴影</label>
              <label><input data-testid="cad-ao" type="checkbox" checked={aoOn}
                            onChange={(e) => applyAO(e.target.checked)} /> SSAO（单视图）</label>
              {aoOn && <label className="geo-cad-slider">
                <span>AO 强度</span><output>{studioLighting.aoIntensity}</output>
                <input type="range" aria-label="AO 强度" data-testid="cad-ao-intensity" min={0} max={3} step={0.1}
                  value={studioLighting.aoIntensity} onChange={e => changeStudioLighting({aoIntensity: e.target.valueAsNumber})} />
              </label>}
              <label>环境光
                <input data-testid="cad-ambient" type="range" min="0" max="4" step="0.1"
                       value={materialSettings.ambientIntensity}
                       onChange={(e) => setMaterialValue("ambientIntensity", Number(e.target.value))} />
              </label>
              <label>直射光
                <input data-testid="cad-direct" type="range" min="0" max="4" step="0.1"
                       value={materialSettings.directIntensity}
                       onChange={(e) => setMaterialValue("directIntensity", Number(e.target.value))} />
              </label>
              <label>金属度
                <input data-testid="cad-metalness" type="range" min="0" max="1" step="0.05"
                       value={materialSettings.metalness}
                       onChange={(e) => setMaterialValue("metalness", Number(e.target.value))} />
              </label>
              <label>粗糙度
                <input data-testid="cad-roughness" type="range" min="0" max="1" step="0.05"
                       value={materialSettings.roughness}
                       onChange={(e) => setMaterialValue("roughness", Number(e.target.value))} />
              </label>
              <details className="geo-render-settings">
                <summary>表面材质</summary>
                <p className="geo-render-feedback">{selectedName ? `应用到 ${selectedName}` : "应用到整个算例"}</p>
                <label>材质预设
                  <select data-testid="cad-surface-preset" value={visibleSurfacePreset}
                    onChange={e => changeSurface(CAD_SURFACES[e.target.value].values)}>
                    {Object.entries(CAD_SURFACES).map(([id, preset]) => <option key={id} value={id}>{preset.label}</option>)}
                    <option value="custom" disabled>自定义</option>
                  </select>
                </label>
                {(Object.keys(CAD_SURFACE_FIELDS) as (keyof CadSurfaceSettings)[]).map(key => {
                  const field = CAD_SURFACE_FIELDS[key];
                  return <label className="geo-cad-slider" key={key}>
                    <span>{field.label}</span><output>{visibleSurface[key]}</output>
                    <input type="range" aria-label={field.label} data-testid={`cad-surface-${key}`}
                      min={field.min} max={field.max} step={0.01} value={visibleSurface[key]}
                      onChange={e => changeSurface({...visibleSurface, [key]: e.target.valueAsNumber})} />
                  </label>;
                })}
              </details>
            </div>
            <div style={s.section}>
              <label><input type="checkbox" checked={explodeOn} disabled={!objects.length}
                            onChange={(e) => toggleExplode(e.target.checked)} /> 爆炸</label>
              <label className="geo-motion-setting">分离程度
                <input data-testid="cad-explode" data-explode={explodeOn ? explodeAmount : 0}
                       type="range" min="0" max="1" step="0.05" value={explodeAmount}
                       aria-valuetext={`${Math.round(explodeAmount * 100)}%`} disabled={!objects.length}
                       onChange={(e) => setExplodeAmount(Number(e.target.value))} />
              </label>
              <label><input data-testid="cad-explode-animate" type="checkbox" checked={explodeAnimated && !reduceExplodeMotion}
                            disabled={reduceExplodeMotion || !objects.length}
                            onChange={(e) => setExplodeAnimated(e.target.checked)} /> 动画过渡</label>
              {reduceExplodeMotion && <p className="geo-motion-note">已减少动态效果，直接切换到目标位置。</p>}
              <label className="geo-motion-setting">时长（秒）
                <input type="number" min="0.1" max="10" step="0.1" value={explodeDurationDraft}
                       disabled={!explodeAnimated || reduceExplodeMotion || !objects.length}
                       aria-invalid={!validExplodeDuration}
                       aria-describedby={!validExplodeDuration ? "geo-explode-duration-error" : undefined}
                       onChange={(e) => {
                         const value = e.target.value;
                         const duration = Number(value);
                         setExplodeDurationDraft(value);
                         if (duration >= 0.1 && duration <= 10) setExplodeDuration(duration);
                       }} />
              </label>
              {!validExplodeDuration && <p id="geo-explode-duration-error" className="geo-motion-note" role="status">
                请输入 0.1 到 10 秒，继续使用上次有效时长。
              </p>}
              <label className="geo-motion-setting">缓动
                <select value={explodeEasing} disabled={!explodeAnimated || reduceExplodeMotion || !objects.length}
                        onChange={(e) => setExplodeEasing(e.target.value as ExplodeEasing)}>
                  <option value="linear">匀速</option><option value="ease-in-out">缓入缓出</option>
                  <option value="ease-out">缓出</option>
                </select>
              </label>
              <button type="button" style={s.miniBtn} disabled={explodePhase === "idle"}
                      onClick={() => changeExplodePhase(explodePhase === "paused" ? "playing" : "paused")}>
                {explodePhase === "paused" ? "继续过渡" : "暂停过渡"}
              </button>
            </div>
            <div style={s.section}>
              <label className="geo-clip-setting">剖切轴
                <select data-testid="cad-clip-axis" disabled={!objects.length} value={clipAxis}
                        onChange={(e) => setClipAxis(e.target.value)}>
                  <option value="">关闭</option><option value="x">X</option>
                  <option value="y">Y</option><option value="z">Z</option>
                </select>
              </label>
              <label className="geo-clip-setting">保留侧
                <select data-testid="cad-clip-side" disabled={!clipAxis || !objects.length} value={clipSide}
                        onChange={(e) => setClipSide(e.target.value)}>
                  <option value="lower">较小坐标</option><option value="higher">较大坐标</option>
                </select>
              </label>
              <label className="geo-clip-setting">切面位置
                <input data-testid="cad-clip-pos" type="range" min="0.02" max="0.98" step="0.01"
                       disabled={!clipAxis || !objects.length}
                       aria-valuetext={`${Math.round(clipPos * 100)}%`} value={clipPos}
                       onChange={(e) => setClipPos(Number(e.target.value))} />
              </label>
              {clipOpenContours && <p className="geo-clip-notice" role="status">截面轮廓未闭合，已显示剖切后的表面。</p>}
            </div>
            <div style={s.section}>
              <label><input data-testid="cad-zebra" type="checkbox" checked={zebraOn}
                            onChange={(e) => setZebraOn(e.target.checked)} /> Zebra</label>
              <label>条纹数
                <input data-testid="cad-zebra-count" type="number" min="2" max="50" step="1"
                       value={zebraCountDraft} onChange={(e) => {
                         setZebraCountDraft(e.target.value);
                         const count = e.target.valueAsNumber;
                         if (Number.isInteger(count) && count >= 2 && count <= 50) setZebraCount(count);
                       }} onBlur={() => setZebraCountDraft(String(zebraCount))} />
              </label>
              <label>条纹不透明度
                <input data-testid="cad-zebra-opacity" type="range" min="0" max="1" step="0.05"
                       value={zebraOpacity} onChange={(e) => setZebraOpacity(Number(e.target.value))} />
              </label>
              <label>配色
                <select data-testid="cad-zebra-scheme" value={zebraScheme}
                        onChange={(e) => setZebraScheme(normalizeZebraScheme(e.target.value))}>
                  <option value="blackwhite">blackwhite</option>
                  <option value="colorful">colorful</option>
                  <option value="grayscale">grayscale</option>
                  <option value="custom">自定义</option>
                </select>
              </label>
              {zebraScheme === "custom" && <div className="geo-render-settings">
                <label>第一种条纹颜色
                  <input data-testid="cad-zebra-color-first" type="color" value={zebraColors[0]}
                         onChange={e => setZebraColors([e.target.value, zebraColors[1]])} />
                </label>
                <label>第二种条纹颜色
                  <input data-testid="cad-zebra-color-second" type="color" value={zebraColors[1]}
                         onChange={e => setZebraColors([zebraColors[0], e.target.value])} />
                </label>
              </div>}
              <label>映射
                <select data-testid="cad-zebra-mapping" value={zebraMapping}
                        onChange={(e) => setZebraMapping(normalizeZebraMapping(e.target.value))}>
                  <option value="normal">normal</option>
                  <option value="reflection">reflection</option>
                </select>
              </label>
              <label>角度
                <input data-testid="cad-zebra-angle" type="range" min="0" max="360" step="5"
                       value={zebraAngle} onChange={(e) => setZebraAngle(normalizeZebraAngle(Number(e.target.value)))} />
              </label>
            </div>
            <div style={s.section}>
              <label><input data-testid="cad-anim" type="checkbox" checked={animOn}
                            onChange={toggleAnimation} /> CAD 动画</label>
              <label>重复 <input type="checkbox" checked={animRepeat}
                              onChange={(e) => setAnimRepeat(e.target.checked)} /></label>
              <label>rx <input data-testid="cad-anim-rx" type="number" step="0.1" value={animRx}
                               onChange={(e) => setAnimRx(Number(e.target.value))} /></label>
              <label>ry <input data-testid="cad-anim-ry" type="number" step="0.1" value={animRy}
                               onChange={(e) => setAnimRy(Number(e.target.value))} /></label>
              <label>rz <input data-testid="cad-anim-rz" type="number" step="0.1" value={animRz}
                               onChange={(e) => setAnimRz(Number(e.target.value))} /></label>
              <label>tx <input data-testid="cad-anim-tx" type="number" step="0.1" value={animTx}
                               onChange={(e) => setAnimTx(Number(e.target.value))} /></label>
              <label>ty <input data-testid="cad-anim-ty" type="number" step="0.1" value={animTy}
                               onChange={(e) => setAnimTy(Number(e.target.value))} /></label>
              <label>tz <input data-testid="cad-anim-tz" type="number" step="0.1" value={animTz}
                               onChange={(e) => setAnimTz(Number(e.target.value))} /></label>
            </div>
            <div style={s.section}>
              <strong>关键帧轨道</strong>
              <textarea ref={trackInputRef} data-testid="cad-track-json" aria-label="关键帧 JSON"
                        value={trackText} onChange={(e) => setTrackText(e.target.value)}
                        spellCheck={false} className="geo-track-editor" />
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                <button type="button" data-testid="cad-track-apply" style={s.miniBtn}
                        disabled={!objects.length} onClick={applyTrackDocument}>应用轨道</button>
                <button type="button" data-testid="cad-track-play" style={s.miniBtn}
                        disabled={!trackDoc || trackPlaying} onClick={playTrack}>播放轨道</button>
                <button type="button" data-testid="cad-track-pause" style={s.miniBtn}
                        disabled={!trackPlaying} onClick={pauseTrack}>暂停轨道</button>
                <button type="button" data-testid="cad-track-stop" style={s.miniBtn}
                        disabled={!trackDoc} onClick={stopTrack}>停止轨道</button>
                <button type="button" data-testid="cad-track-export" style={s.miniBtn} onClick={exportTrack}>导出轨道</button>
              </div>
              <input data-testid="cad-track-time" aria-label="轨道时间" disabled={!trackDoc}
                     aria-valuetext={`${trackTime.toFixed(2)} 秒`} type="range" min="0" max={trackDoc?.duration ?? 1} step="0.01"
                     value={trackTime} onChange={(e) => {
                       const value = Number(e.target.value);
                       if (!trackPlayingRef.current) {
                         stopExplode();
                         animStateRef.current.on = false;
                         setAnimOn(false);
                       }
                       applyTrackAtTime(value);
                     }} />
              <span data-testid="cad-track-status" role="status" className="geo-motion-note">{trackStatus}</span>
            </div>
          </div>
        )}
        <div ref={mountRef} data-testid="geo-canvas-host" data-edges={edgesOn ? "true" : "false"}
             data-transparent={transparentOn ? "true" : "false"} data-theme={theme}
             data-ortho={visibleProjection ? "true" : "false"} data-control={visibleControl}
             data-view-mode={viewMode} data-studio={studioOn ? "true" : "false"}
             data-shadow={shadowOn ? "true" : "false"} data-ao={aoOn ? "true" : "false"}
             data-clip-caps={clipCapCount} data-explode-progress={explodeProgress.toFixed(3)} data-explode-phase={explodePhase}
             data-track-playing={trackPlaying ? "true" : "false"} data-track-time={trackTime.toFixed(3)}
             data-track-duration={trackDoc?.duration ?? 0} data-track-count={trackDoc?.tracks.length ?? 0}
             data-zebra-scheme={zebraScheme} data-zebra-mapping={zebraMapping} data-zebra-angle={zebraAngle}
             data-ambient={materialSettings.ambientIntensity} data-direct={materialSettings.directIntensity}
             data-metalness={materialSettings.metalness} data-roughness={materialSettings.roughness}
             className="geo-canvas">
          {viewMode === "quad" && <div className="geo-quad-views">
            {CAD_QUAD_PRESETS.map((preset, index) => <div key={preset}
              ref={element => { quadElementsRef.current[index] = element; }}
              className="geo-quad-view"
              data-testid={`cad-viewport-${index}`} data-active={activeQuad === index}
              role="group" tabIndex={0} aria-label={`${CAD_VIEW_PRESETS[preset]} 视口`}
              onPointerDown={() => refreshQuadSelection(index)}
              onFocus={() => refreshQuadSelection(index)} onWheel={() => refreshQuadSelection(index)}>
              <span>{CAD_VIEW_PRESETS[quadViewsRef.current[index]?.preset ?? preset]}</span>
            </div>)}
          </div>}
        </div>
      </div>
      {!currentCase && (
        <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center",
                      justifyContent: "center", background: "#0f1115cc", color: "#8b93a7" }}>
          选择一个算例后预览几何
        </div>
      )}
    </div>
  );
}

const s: Record<string, React.CSSProperties> = {
  hint: { padding: 16, color: "#8b93a7" },
  btn: { padding: "8px 12px", background: "var(--vf-active)", color: "var(--vf-text)", border: "1px solid var(--vf-accent)", borderRadius: 4, cursor: "pointer", fontSize: 14, minHeight: 44 },
  miniBtn: { padding: "8px 10px", background: "var(--vf-bg)", color: "var(--vf-text)", border: "1px solid var(--vf-border)", borderRadius: 4, cursor: "pointer", fontSize: 14, minHeight: 44 },
  section: { borderTop: "1px solid #2a2f3a", marginTop: 8, paddingTop: 8, display: "grid", gap: 5 },
};

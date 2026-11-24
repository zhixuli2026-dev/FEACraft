import { MathUtils, OrthographicCamera, PerspectiveCamera, Vector3 } from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { TrackballControls } from "three/examples/jsm/controls/TrackballControls.js";
import { cadFitDistance } from "./cadScene";
import { CAD_VIEW_DIRECTIONS, cadOrthoSpan } from "./cadView";
import type { CadControl, CadViewPreset } from "./cadView";

export type CadCamera = PerspectiveCamera | OrthographicCamera;
export type CadCameraControls = OrbitControls | TrackballControls;

export function makeCadControls(camera: CadCamera, element: HTMLElement, mode: CadControl): CadCameraControls {
  return mode === "trackball" ? new TrackballControls(camera, element) : new OrbitControls(camera, element);
}

export class CadQuadView {
  camera: CadCamera = new PerspectiveCamera(45, 1, 0.1, 5000);
  mode: CadControl = "orbit";
  preset: CadViewPreset;
  private controls?: CadCameraControls;
  private element?: HTMLElement;
  private target = new Vector3();
  private width = 1;
  private height = 1;
  private span = 1;

  constructor(preset: CadViewPreset) {
    this.preset = preset;
  }

  get orthographic() {
    return this.camera instanceof OrthographicCamera;
  }

  attach(element: HTMLElement) {
    this.detach();
    this.element = element;
    this.reconnect();
  }

  detach() {
    if (this.controls) {
      this.target.copy(this.controls.target);
      this.controls.dispose();
      this.controls = undefined;
    }
    this.element = undefined;
  }

  setMode(mode: CadControl) {
    if (this.mode === mode) return;
    this.mode = mode;
    this.reconnect();
  }

  setProjection(orthographic: boolean) {
    if (orthographic === this.orthographic) return;
    const prior = this.camera;
    const target = this.controls?.target ?? this.target;
    const distance = prior.position.distanceTo(target);
    const next = orthographic ? new OrthographicCamera(-1, 1, 1, -1, prior.near, prior.far)
      : new PerspectiveCamera(45, this.width / this.height, prior.near, prior.far);
    next.position.copy(prior.position);
    next.up.copy(prior.up);
    next.quaternion.copy(prior.quaternion);
    const tangent = Math.tan(MathUtils.degToRad(45 / 2));
    if (orthographic) this.span = Math.max(0.0001, 2 * distance * tangent / prior.zoom);
    else next.zoom = Math.max(0.0001, 2 * distance * tangent * prior.zoom / this.span);
    this.camera = next;
    this.updateProjection();
    this.reconnect();
  }

  fit(center: Vector3, size: Vector3, preset = this.preset) {
    this.preset = preset;
    const aspect = this.width / this.height;
    const diameter = size.length();
    const distance = cadFitDistance(size.toArray(), 45, aspect);
    this.span = cadOrthoSpan(size.toArray(), aspect);
    this.camera.position.copy(center).addScaledVector(new Vector3(...CAD_VIEW_DIRECTIONS[preset]).normalize(), distance);
    const up = new Vector3(0, preset === "y" ? 0 : 1, preset === "y" ? 1 : 0);
    const differentUp = !this.camera.up.equals(up);
    this.camera.up.copy(up);
    this.camera.zoom = 1;
    this.camera.far = Math.max(50, distance * 4, diameter * 3);
    this.camera.lookAt(center);
    this.target.copy(center);
    this.controls?.target.copy(center);
    this.updateProjection();
    if (differentUp) this.reconnect();
    else this.controls?.update();
  }

  resize(width: number, height: number) {
    if (width === this.width && height === this.height) return;
    this.width = width;
    this.height = height;
    this.updateProjection();
    if (this.controls instanceof TrackballControls) this.controls.handleResize();
  }

  update() {
    this.controls?.update();
  }

  private updateProjection() {
    const aspect = this.width / this.height;
    const camera = this.camera;
    if (camera instanceof OrthographicCamera) {
      camera.left = -this.span * aspect / 2;
      camera.right = this.span * aspect / 2;
      camera.top = this.span / 2;
      camera.bottom = -this.span / 2;
    } else camera.aspect = aspect;
    camera.updateProjectionMatrix();
  }

  private reconnect() {
    const element = this.element;
    if (!element) return;
    this.detach();
    this.element = element;
    this.controls = makeCadControls(this.camera, element, this.mode);
    this.controls.target.copy(this.target);
    this.controls.update();
  }
}

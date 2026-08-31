export class Door {
  constructor() { this.open = false; this.collider_enabled = true; }
  activate() {
    this.open = true;
    this.collider_enabled = false;
  }
}

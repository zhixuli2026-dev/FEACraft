import { normalizeCrop } from "../web/src/utils/screenCrop.ts";
import assert from "node:assert/strict";

assert.deepEqual(normalizeCrop({x: 10, y: 20}, {x: 30, y: 50}, 100, 100),
                 {x: 10, y: 20, width: 20, height: 30});
assert.deepEqual(normalizeCrop({x: 90, y: 80}, {x: 10, y: 20}, 100, 100),
                 {x: 10, y: 20, width: 80, height: 60});
assert.deepEqual(normalizeCrop({x: -10, y: -20}, {x: 120, y: 130}, 100, 100),
                 {x: 0, y: 0, width: 100, height: 100});
assert.equal(normalizeCrop({x: 2, y: 2}, {x: 2, y: 2}, 100, 100).width, 1);
assert.throws(() => normalizeCrop({x: 0, y: 0}, {x: 1, y: 1}, 0, 100));
console.log("E20 screen crop forward/reverse/clamp/min-size and invalid dimensions: OK");

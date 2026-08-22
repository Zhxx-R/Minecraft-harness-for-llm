import assert from "node:assert/strict";
import test from "node:test";
import { Vec3 } from "vec3";
import {
  entityMetadataDelta,
  entityServerDetails
} from "./entity-details.js";

test("maps server metadata through versioned registry keys without entity-specific code", () => {
  const bot = {
    version: "1.20.1",
    registry: {
      entities: {
        82: {
          id: 82,
          name: "sheep",
          displayName: "Sheep",
          type: "animal",
          category: "Passive mobs",
          width: 0.9,
          height: 1.3,
          metadataKeys: ["shared_flags", "health", "baby", "wool"]
        }
      }
    }
  };
  const entity = {
    id: 68,
    entityType: 82,
    name: "sheep",
    displayName: "Sheep",
    type: "animal",
    kind: "Passive mobs",
    uuid: "entity-68",
    width: 0.9,
    height: 1.3,
    onGround: true,
    isValid: true,
    health: 8,
    velocity: new Vec3(0, 0, 0),
    equipment: [],
    effects: {},
    metadata: [0, 8, false, 16]
  };

  const details = entityServerDetails(bot as never, entity as never);

  assert.equal(details.source, "minecraft_server_entity_packets_and_versioned_registry");
  assert.equal(details.entity_type_id, 82);
  assert.equal(details.registry_category, "Passive mobs");
  assert.deepEqual(details.metadata, {
    shared_flags: 0,
    health: 8,
    baby: false,
    wool: 16
  });
  assert.deepEqual(details.metadata_decoded, {
    shared_flags: {
      on_fire: false,
      crouching: false,
      sprinting: false,
      swimming: false,
      invisible: false,
      glowing: false,
      elytra_flying: false
    },
    wool: {
      color_id: 0,
      color: "white",
      is_sheared: true
    }
  });
  assert.deepEqual(
    (details.metadata_decoder as Record<string, unknown>).recognized_fields,
    ["shared_flags", "wool"]
  );
  assert.deepEqual(
    (details.metadata_decoder as Record<string, unknown>).explicit_fields,
    ["shared_flags", "wool"]
  );
  assert.deepEqual(
    (details.metadata_decoder as Record<string, unknown>).defaulted_fields,
    []
  );
});

test("fills schema defaults when Mineflayer omits unchanged sheep metadata", () => {
  const bot = {
    version: "1.20.1",
    registry: {
      entities: {
        82: {
          name: "sheep",
          metadataKeys: ["shared_flags", "health", "baby", "wool"]
        }
      }
    }
  };
  const sparseMetadata = new Array(4);
  sparseMetadata[1] = 8;
  const entity = {
    id: 68,
    entityType: 82,
    name: "sheep",
    metadata: sparseMetadata,
    equipment: [],
    effects: {}
  };

  const details = entityServerDetails(bot as never, entity as never);

  assert.deepEqual(details.metadata, { health: 8 });
  assert.deepEqual(details.metadata_decoded, {
    shared_flags: {
      on_fire: false,
      crouching: false,
      sprinting: false,
      swimming: false,
      invisible: false,
      glowing: false,
      elytra_flying: false
    },
    wool: {
      color_id: 0,
      color: "white",
      is_sheared: false
    }
  });
  assert.deepEqual(
    (details.metadata_decoder as Record<string, unknown>).explicit_fields,
    []
  );
  assert.deepEqual(
    (details.metadata_decoder as Record<string, unknown>).defaulted_fields,
    ["shared_flags", "wool"]
  );
});

test("decodes sheep color and sheared state from the packed wool byte", () => {
  const bot = {
    version: "1.20.1",
    registry: {
      entities: {
        82: {
          name: "sheep",
          metadataKeys: ["shared_flags", "wool"]
        }
      }
    }
  };
  const entity = {
    id: 68,
    entityType: 82,
    name: "sheep",
    metadata: [0, 28],
    equipment: [],
    effects: {}
  };

  const details = entityServerDetails(bot as never, entity as never);

  assert.deepEqual(
    (details.metadata_decoded as Record<string, unknown>).wool,
    {
      color_id: 12,
      color: "brown",
      is_sheared: true
    }
  );
});

test("decodes zombie fire as a shared entity flag", () => {
  const bot = {
    version: "1.20.1",
    registry: {
      entities: {
        107: {
          name: "zombie",
          metadataKeys: ["shared_flags", "baby", "drowned_conversion"]
        }
      }
    }
  };
  const entity = {
    id: 9,
    entityType: 107,
    name: "zombie",
    metadata: [1, false, -1],
    equipment: [],
    effects: {}
  };

  const details = entityServerDetails(bot as never, entity as never);
  const decoded = details.metadata_decoded as Record<string, Record<string, unknown>>;

  assert.equal(decoded.shared_flags.on_fire, true);
  assert.equal(decoded.shared_flags.crouching, false);
  assert.deepEqual(details.metadata, {
    shared_flags: 1,
    baby: false,
    drowned_conversion: -1
  });
});

test("does not apply 1.20.1 semantic rules to another server version", () => {
  const bot = {
    version: "1.21.1",
    registry: {
      entities: {
        82: {
          name: "sheep",
          metadataKeys: ["shared_flags", "wool"]
        }
      }
    }
  };
  const entity = {
    id: 68,
    entityType: 82,
    name: "sheep",
    metadata: [0, 28],
    equipment: [],
    effects: {}
  };

  const details = entityServerDetails(bot as never, entity as never);

  assert.deepEqual(details.metadata_decoded, {});
  assert.equal(
    (details.metadata_decoder as Record<string, unknown>).available,
    false
  );
});

test("reports generic named metadata changes for post-interaction evidence", () => {
  assert.deepEqual(
    entityMetadataDelta(
      { metadata: { health: 8, wool: 0 } },
      { metadata: { health: 8, wool: 16 } }
    ),
    {
      wool: {
        before: 0,
        after: 16
      }
    }
  );
});

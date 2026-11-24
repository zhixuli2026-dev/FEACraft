import { useMemo } from "react";
import type { CadSceneGroup, CadSceneObject } from "../utils/cadScene";

export interface CadObjectState {
  key: string;
  name: string;
  visible: boolean;
  bbox: CadSceneObject["bbox"];
  vertices: number;
  faces: number;
  groups?: CadSceneGroup[];
}

type Branch = { group: CadSceneGroup; children: TreeItem[]; targets: CadObjectState[] };
type TreeItem = Branch | CadObjectState;

function organize(objects: CadObjectState[]): TreeItem[] {
  const root: TreeItem[] = [];
  const branches = new Map<string, Branch>();
  for (const object of objects) {
    let parent = root;
    for (const group of object.groups ?? []) {
      let branch = branches.get(group.id);
      if (!branch) {
        branch = {group, children: [], targets: []};
        branches.set(group.id, branch);
        parent.push(branch);
      }
      branch.targets.push(object);
      parent = branch.children;
    }
    parent.push(object);
  }
  return root;
}

interface Props {
  objects: CadObjectState[];
  selected: string;
  collapsed: string[];
  onSelect: (name: string) => void;
  onToggleVisibility: (names: string[]) => void;
  onToggleGroup: (id: string) => void;
}

export default function CadObjectTree({objects, selected, collapsed, onSelect, onToggleVisibility, onToggleGroup}: Props) {
  const tree = useMemo(() => organize(objects), [objects]);
  const render = (items: TreeItem[]) => items.map((item) => {
    if ("group" in item) {
      const {id, name} = item.group;
      const expanded = !collapsed.includes(id);
      const count = item.targets.filter((object) => object.visible).length;
      return <li key={id}>
        <div className="geo-object-row">
          <input type="checkbox" aria-label={`显示装配 ${name}`} checked={count === item.targets.length}
                 ref={(input) => { if (input) input.indeterminate = count > 0 && count < item.targets.length; }}
                 onChange={() => onToggleVisibility(item.targets.map((obj) => obj.name))} />
          <button type="button" className="geo-object-button geo-branch-button" aria-expanded={expanded}
                  onClick={() => onToggleGroup(id)}>{name}</button>
        </div>
        {expanded && <ul className="geo-object-branch">{render(item.children)}</ul>}
      </li>;
    }
    const active = selected === item.name;
    return <li key={item.key} data-object-name={item.name} data-visible={item.visible} data-selected={active}>
      <div className="geo-object-row">
        <input type="checkbox" aria-label={`显示 ${item.name}`} checked={item.visible}
               onChange={() => onToggleVisibility([item.name])} />
        <button type="button" className="geo-object-button" onClick={() => onSelect(item.name)} aria-pressed={active}>
          <span>{item.name}</span>
        </button>
      </div>
    </li>;
  });
  return tree.length ? <ul className="geo-object-list" aria-label="CAD 对象">{render(tree)}</ul>
    : <div className="geo-empty-objects">（无对象）</div>;
}

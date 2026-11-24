type Domain = { type: string; attributes: Record<string, string>; children?: Domain[] };
type ProxyChoice = { type: string; label: string; properties: NativeProperty[] };
export type NativeProperty = { name: string; type: string; attributes: Record<string, string>; domains: Domain[];
  multiple?: boolean; proxy_choices?: ProxyChoice[] };
type ProxyDraft = { type: string; properties: PropertyDraft };
export type PropertyDraft = { [key: string]: string | ProxyDraft };
export type ArrayInfo = { name: string; association: "点" | "单元" | "全局" | "行"; components: number };
const arrayAssociations = { "点": "POINTS", "单元": "CELLS", "全局": "FIELD", "行": "ROWS" } as const;

export function editable(property: NativeProperty) {
  const { attributes, domains, type } = property;
  return ["DoubleVectorProperty", "IntVectorProperty", "IdTypeVectorProperty", "StringVectorProperty", "ProxyProperty"].includes(type)
    && attributes.is_internal !== "1" && attributes.information_only !== "1" && attributes.panel_visibility !== "never"
    && !domains.some(domain => domain.type === "FileListDomain");
}

export const arrayField = (property: NativeProperty) => property.domains.some(domain => domain.type === "ArrayListDomain");
const repeatable = (property: NativeProperty) => property.attributes.repeatable === "1" || property.attributes.repeat_command === "1";
const hasDomain = (property: NativeProperty, type: string) => property.domains.some(domain => domain.type === type);
const arrayNameOnly = (property: NativeProperty) => property.type === "StringVectorProperty" && property.attributes.number_of_elements === "1";

export function serializeProperties(fields: NativeProperty[], draft: PropertyDraft): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const property of fields) {
    const value = draft[property.name];
    if (value === undefined || (typeof value === "string" && !value.trim())) continue;
    result[property.name] = fieldValue(property, value);
  }
  return result;
}

function fieldValue(property: NativeProperty, value: PropertyDraft[string]): unknown {
  if (typeof value !== "string") {
    const choice = property.proxy_choices?.find(option => option.type === value.type);
    if (!choice) throw new Error(`${property.name} 的类型已不可用`);
    return { type: value.type, properties: serializeProperties(choice.properties.filter(editable), value.properties) };
  }
  if (property.type === "ProxyProperty" || (arrayField(property) && (!arrayNameOnly(property) || repeatable(property)))) return JSON.parse(value);
  if (property.type === "StringVectorProperty" && repeatable(property)) return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  if (property.type === "StringVectorProperty" || hasDomain(property, "EnumerationDomain") || hasDomain(property, "FieldDataDomain")) return value;
  const numbers = value.trim().split(/[\s,，]+/).map(Number);
  const required = Number(property.attributes.number_of_elements || 0);
  const variable = repeatable(property);
  if (numbers.some(number => !Number.isFinite(number) || (property.type !== "DoubleVectorProperty" && !Number.isInteger(number)))
      || (!variable && required > 0 && numbers.length !== required)) {
    throw new Error(`${property.name} 的数值或数量不符合属性定义`);
  }
  return numbers.length === 1 && !variable ? numbers[0] : numbers;
}

export default function ParaViewPropertyFields({ fields, values, update, disabled, arrays, hasInput }: {
  fields: NativeProperty[]; values: PropertyDraft; update: (next: PropertyDraft) => void;
  disabled: boolean; arrays?: ArrayInfo[] | null; hasInput: boolean;
}) {
  return <div className="vf-pv-fields">
    {fields.map(property => {
      const label = property.attributes.label || property.name;
      const value = values[property.name] ?? "";
      const set = (next: PropertyDraft[string]) => update({ ...values, [property.name]: next });
      const options = property.domains.find(domain => domain.type === "EnumerationDomain")?.children
        ?.filter(entry => entry.type === "Entry" && entry.attributes.text);
      if (property.proxy_choices?.length) {
        const selected = typeof value !== "string" ? value : undefined;
        const choice = property.proxy_choices.find(option => option.type === selected?.type);
        return <fieldset key={property.name} className="vf-pv-composite" title={property.name}>
          <legend>{label}</legend>
          <select aria-label={label} value={choice?.type ?? ""} disabled={disabled}
            onChange={event => set(event.target.value ? { type: event.target.value, properties: {} } : "")}>
            <option value="">使用默认类型</option>
            {property.proxy_choices.map(option => <option key={option.type} value={option.type}>{option.label}</option>)}
          </select>
          {choice && selected && <ParaViewPropertyFields fields={choice.properties.filter(editable)}
            values={selected.properties} update={properties => set({ type: selected.type, properties })}
            disabled={disabled} arrays={arrays} hasInput={hasInput} />}
        </fieldset>;
      }
      const scalar = typeof value === "string" ? value : "";
      if (arrayField(property) && repeatable(property) && arrays !== undefined) {
        const association = property.name === "SelectArrays" && arrays?.some(array => array.association === "行")
          ? "行" : property.name.startsWith("Cell") ? "单元" : "点";
        const choices = arrays?.filter(array => array.association === association) ?? [];
        const selected = scalar ? JSON.parse(scalar) as string[] : [];
        return <label key={property.name} title={property.name}>{label}
          <select aria-label={label} value={selected} multiple size={Math.max(2, Math.min(choices.length, 5))}
            disabled={disabled || !choices.length}
            onChange={event => set(JSON.stringify(Array.from(event.target.selectedOptions, option => option.value)))}>
            {choices.map(array => <option key={array.name} value={array.name}>{array.name} · {array.components} 分量</option>)}
          </select>
        </label>;
      }
      if (arrayField(property) && arrays !== undefined) return <label key={property.name} title={property.name}>{label}
        <select aria-label={label} value={scalar} disabled={disabled || !arrays} onChange={event => set(event.target.value)}>
          <option value="">{!hasInput ? "选择输入数据集" : arrays ? "使用默认字段" : "等待读取字段"}</option>
          {arrays?.map(array => {
            const key = arrayNameOnly(property) ? array.name : JSON.stringify([arrayAssociations[array.association], array.name]);
            return <option value={key} key={key}>{array.name} · {array.association} · {array.components} 分量</option>;
          })}
        </select>
      </label>;
      if (hasDomain(property, "FieldDataDomain")) return <label key={property.name} title={property.name}>{label}
        <select aria-label={label} value={scalar} disabled={disabled} onChange={event => set(event.target.value)}>
          <option value="">使用默认值</option><option value="Point Data">点数据</option><option value="Cell Data">单元数据</option>
        </select>
      </label>;
      return <label key={property.name} title={property.name}>{label}
        {options?.length ? <select aria-label={label} value={scalar} disabled={disabled} onChange={event => set(event.target.value)}>
          <option value="">使用默认值</option>
          {options.map(option => <option key={option.attributes.value} value={option.attributes.text}>{option.attributes.text}</option>)}
        </select> : hasDomain(property, "BooleanDomain") ? <select aria-label={label}
          value={scalar} disabled={disabled} onChange={event => set(event.target.value)}>
          <option value="">使用默认值</option><option value="1">启用</option><option value="0">关闭</option>
        </select> : property.type === "StringVectorProperty" && repeatable(property) ? <textarea aria-label={label}
          value={scalar} rows={3} disabled={disabled} placeholder={arrayField(property) ? '["字段一","字段二"]' : "每行一个值"}
          onChange={event => set(event.target.value)} />
        : <input aria-label={label} value={scalar} disabled={disabled}
          placeholder={arrayField(property) ? '["POINTS","场名"]' : property.attributes.default_values}
          onChange={event => set(event.target.value)} />}
      </label>;
    })}
  </div>;
}

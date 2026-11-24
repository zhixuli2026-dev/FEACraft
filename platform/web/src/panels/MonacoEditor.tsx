import { useEffect, useRef } from "react";
import * as monaco from "monaco-editor";

interface MonacoEditorProps {
  value: string;
  language: string;
  theme: string;
  onChange: (value: string) => void;
  onMount: (editor: monaco.editor.IStandaloneCodeEditor, monacoApi: typeof monaco) => void;
  options?: monaco.editor.IStandaloneEditorConstructionOptions;
}

/** 直接使用本地 Monaco ESM；不经过 @monaco-editor/react 的 AMD/CDN loader。 */
export default function MonacoEditor({ value, language, theme, onChange, onMount, options }: MonacoEditorProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const changeRef = useRef(onChange);
  const mountRef = useRef(onMount);
  const applyingValue = useRef(false);
  changeRef.current = onChange;
  mountRef.current = onMount;

  useEffect(() => {
    const editor = monaco.editor.create(hostRef.current!, {
      ...options, value, language, theme,
    });
    editorRef.current = editor;
    const subscription = editor.onDidChangeModelContent(() => {
      if (!applyingValue.current) changeRef.current(editor.getValue());
    });
    mountRef.current(editor, monaco);
    return () => {
      subscription.dispose();
      const model = editor.getModel();
      editor.dispose();
      model?.dispose();
      editorRef.current = null;
    };
  }, []);

  useEffect(() => { monaco.editor.setTheme(theme); }, [theme]);
  useEffect(() => {
    const model = editorRef.current?.getModel();
    if (model) monaco.editor.setModelLanguage(model, language);
  }, [language]);
  useEffect(() => {
    const editor = editorRef.current;
    if (editor && editor.getValue() !== value) {
      applyingValue.current = true;
      try { editor.setValue(value); }
      finally { applyingValue.current = false; }
    }
  }, [value]);

  return <div ref={hostRef} style={{ height: "100%", width: "100%" }} />;
}

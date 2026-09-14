declare namespace JSX {
  interface IntrinsicElements { [elemName: string]: any; }
}

declare module "react" {
  export function useState<T>(initial: T): [T, (value: T | ((previous: T) => T)) => void];
  export function useEffect(effect: () => void | (() => void), deps?: readonly unknown[]): void;
  export function useLayoutEffect(effect: () => void | (() => void), deps?: readonly unknown[]): void;
  export function useCallback<T extends (...args: any[]) => any>(fn: T, deps: readonly unknown[]): T;
  export function useMemo<T>(factory: () => T, deps: readonly unknown[]): T;
  export function useRef<T>(initial: T): { current: T };
}

declare module "react/jsx-runtime" {
  export const jsx: any;
  export const jsxs: any;
  export const Fragment: any;
}

declare module "@decky/ui" {
  export interface DropdownOption { data: any; label: any; }
  export const ButtonItem: any;
  export const DialogButton: any;
  export const Focusable: any;
  export const ModalRoot: any;
  export const Navigation: any;
  export const showModal: (node: any) => { Close(): void };
  export const DropdownItem: (props: {
    label?: any;
    description?: any;
    rgOptions: DropdownOption[];
    selectedOption: any;
    disabled?: boolean;
    onChange?: (option: DropdownOption) => void;
    [key: string]: any;
  }) => any;
  export const ErrorBoundary: any;
  export const Field: any;
  export const PanelSection: any;
  export const PanelSectionRow: any;
  export const Spinner: any;
  export const TextField: any;
  export const ToggleField: (props: {
    label?: any;
    description?: any;
    checked?: boolean;
    disabled?: boolean;
    onChange?: (checked: boolean) => void;
    [key: string]: any;
  }) => any;
  export const staticClasses: { Title: string };
}

declare module "@decky/api" {
  export const enum FileSelectionType { FILE, FOLDER }
  export interface FilePickerRes { path: string; realpath: string; }
  export const callable: <Args extends any[] = [], Return = void>(route: string) => (...args: Args) => Promise<Return>;
  export const definePlugin: <T extends () => any>(fn: T) => T;
  export const openFilePicker: (
    select: FileSelectionType,
    startPath: string,
    includeFiles?: boolean,
    includeFolders?: boolean,
    filter?: RegExp | ((file: File) => boolean),
    extensions?: string[],
    showHiddenFiles?: boolean,
    allowAllFiles?: boolean,
    max?: number,
  ) => Promise<FilePickerRes>;
  export const toaster: { toast(data: {title: unknown; body: unknown}): unknown };
  export const useQuickAccessVisible: () => boolean;
}

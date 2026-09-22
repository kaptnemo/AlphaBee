// CSS 模块类型声明。
// 让 TypeScript 识别 `import "./globals.css"` 这类副作用导入，
// 以及 `import styles from "./xxx.module.css"` 的 CSS Module 导入。
declare module "*.css";

declare module "*.module.css" {
  const classes: { readonly [key: string]: string };
  export default classes;
}

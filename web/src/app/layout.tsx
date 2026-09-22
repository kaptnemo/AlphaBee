import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AlphaBee 投资分析",
  description: "AlphaBee 多智能体投资分析系统 Web 界面",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-slate-950 text-slate-100 antialiased">
        {children}
      </body>
    </html>
  );
}

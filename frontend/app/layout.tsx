import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "../styles/globals.css";

const inter = Inter({
  subsets: ["latin", "cyrillic"],
  display: "swap",
  variable: "--font-sans",
});

export const metadata: Metadata = {
  metadataBase: new URL("http://localhost:3000"),
  title: {
    default: "AutoEdit",
    template: "%s | AutoEdit",
  },
  description:
    "AutoEdit — self-hosted AI Video Editor для автоматического монтажа видео с локальной обработкой, очередями задач и отслеживанием прогресса в реальном времени.",
  applicationName: "AutoEdit",
  keywords: [
    "AutoEdit",
    "AI Video Editor",
    "video editing",
    "self-hosted",
    "Next.js",
    "FastAPI",
    "FFmpeg",
    "OpenCV",
  ],
  authors: [{ name: "AutoEdit" }],
  creator: "AutoEdit",
  publisher: "AutoEdit",
  robots: {
    index: false,
    follow: false,
  },
  icons: {
    icon: [
      {
        url: "/favicon.ico",
      },
    ],
  },
};

export const viewport: Viewport = {
  themeColor: "#0B1020",
  colorScheme: "dark",
};

type RootLayoutProps = Readonly<{
  children: React.ReactNode;
}>;

/**
 * Root layout for the AutoEdit frontend.
 *
 * This file is intentionally minimal but production-oriented:
 * - connects global styles for the whole application;
 * - sets global metadata and dark theme defaults;
 * - provides a stable DOM structure for all App Router pages;
 * - exposes a font CSS variable for use in global styles and UI components.
 */
export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <body className={`${inter.variable} min-h-screen bg-app text-app-text antialiased`}>
        <div id="app-root" className="min-h-screen">
          {children}
        </div>
      </body>
    </html>
  );
}
import type { Metadata } from "next";

import { PlatformSignIn } from "@/components/platform-sign-in";

export const metadata: Metadata = { title: "Sign in" };

export default function Home() {
  return <PlatformSignIn />;
}

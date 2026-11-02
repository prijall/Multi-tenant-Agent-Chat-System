import { NextResponse, type NextRequest } from "next/server";
import { findTenant } from "@/lib/tenants";

// Rewriting to a path with no route renders the app's 404 page with a real 404 status.
export function proxy(request: NextRequest) {
  const slug = request.nextUrl.pathname.split("/")[2];
  if (!findTenant(slug)) {
    return NextResponse.rewrite(new URL("/404", request.url));
  }
  return NextResponse.next();
}

export const config = { matcher: "/a/:tenant/:path*" };

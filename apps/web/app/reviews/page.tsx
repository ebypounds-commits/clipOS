import ReviewsView from "./ReviewsView";

export const dynamic = "force-dynamic";

type PageProps = {
  searchParams: Promise<{ org?: string }>;
};

export default async function ReviewsPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const orgId = params.org ?? process.env.NEXT_PUBLIC_ORG_ID ?? "";
  return <ReviewsView orgId={orgId} />;
}

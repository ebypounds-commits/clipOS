import ReviewsView from "../ReviewsView";

export const dynamic = "force-dynamic";

type PageProps = {
  params: Promise<{ org: string }>;
};

export default async function ReviewsOrgPage({ params }: PageProps) {
  const { org } = await params;
  return <ReviewsView orgId={decodeURIComponent(org)} />;
}

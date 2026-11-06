use strict;
use warnings;
use Test::More;
use JSON::PP qw(encode_json);
use lib 'lib';
BEGIN { use_ok('Routing::Report', qw(read_log render_text render_csv has_issues)) or BAIL_OUT('report module missing'); }

sub parse_lines {
    my (@lines) = @_;
    my $text = join "\n", map { ref $_ ? encode_json($_) : $_ } @lines;
    open my $fh, '<', \$text or die $!;
    return read_log($fh, 'memory');
}
sub event { return { event => $_[0], t_ms => $_[1], @_[2 .. $#_] }; }

open my $fixture, '<', 't/fixtures/mixed.jsonl' or die $!;
my $run = read_log($fixture, 'mixed');
is(scalar @{$run->{failures}}, 3, 'each link failure retained');
is($run->{failures}[0]{convergence_ms}, 35, 'subtracts failure from convergence timestamp');
is($run->{failures}[1]{convergence_ms}, 10, 'second failure has independent duration');
ok(!defined $run->{failures}[2]{convergence_ms}, 'unfinished failure has no invented duration');
is($run->{drops}{packet_drop}{no_route}, 2, 'drops counted by reason');
is($run->{drops}{malformed_packet}{bad_header}, 1, 'bad packets separate from damaged log lines');
is($run->{delivered}, 1, 'delivered packet count');
is_deeply([map { $_->{status} } @{$run->{tests}}], ['pass', 'fail'], 'test outcomes parsed');
is($run->{rejected}, 2, 'corrupt and truncated records rejected');
is($run->{accepted}, 11, 'good records survive bad lines');
is_deeply([map { $_->{line} } @{$run->{diagnostics}}], [12, 13], 'diagnostics contain source line numbers');
ok(has_issues([$run]), 'failed check, incomplete recovery and corrupt input are visible');
like(render_text([$run]), qr/f1.*35 ms/, 'readable measured duration');
like(render_text([$run]), qr/f3.*incomplete/, 'unfinished recovery explicit');
like(render_text([$run]), qr/packet_drop.*no_route.*2/, 'plain report has drop counts');
like(render_text([$run]), qr/lost_probe.*fail/, 'plain report names failed scenario');
like(render_csv([$run]), qr/"convergence".*"f1".*"35"/, 'CSV contains duration');
like(render_csv([$run]), qr/"convergence".*"f3".*"incomplete"/, 'CSV retains unfinished failure');

my $overlap = parse_lines(
    event('link_failure', 0, failure_id=>'a', link=>'A-B'),
    event('link_failure', 1, failure_id=>'b', link=>'B-C'),
    event('routes_converged', 4, failure_id=>'b'),
    event('routes_converged', 10, failure_id=>'a'));
is_deeply([map { $_->{convergence_ms} } @{$overlap->{failures}}], [10,3], 'overlapping failures matched by ID');
ok(!has_issues([$overlap]), 'clean fully recovered run');

my @invalid = ('[]', 'null', '{}', '{',
    event('unknown', 0), event('packet_drop', -1, reason=>'x', node=>'A'),
    event('packet_drop', 0, reason=>[], node=>'A'),
    event('packet_drop', 0, reason=>'', node=>'A'),
    event('packet_drop', 0, reason=>'x'),
    event('test_result', 0, name=>'probe', status=>'maybe'),
    event('link_failure', 0, failure_id=>{}, link=>'A-B'),
    event('packet_delivered', '1.5', node=>'A'),
    event('packet_delivered', JSON::PP::true, node=>'A'),
    event('packet_delivered', '999999999999999999999999', node=>'A'),
    event('packet_drop', 0, node=>'A', reason=>"bad\nheader"));
for my $invalid (@invalid) {
    my $bad = parse_lines($invalid);
    is($bad->{rejected}, 1, 'rejects invalid JSON or invalid event fields');
    is($bad->{accepted}, 0, 'invalid event never counted');
}
my $order = parse_lines(
    event('routes_converged', 1, failure_id=>'orphan'),
    event('link_failure', 10, failure_id=>'a', link=>'A-B'),
    event('link_failure', 11, failure_id=>'a', link=>'B-C'),
    event('routes_converged', 9, failure_id=>'a'),
    event('routes_converged', 12, failure_id=>'a'),
    event('routes_converged', 13, failure_id=>'a'));
is($order->{rejected}, 4, 'orphan, duplicate failure, backward time, duplicate convergence rejected');
is($order->{failures}[0]{convergence_ms}, 2, 'bad events do not poison good pair');
is_deeply($order->{rejection_reasons}, {orphan_convergence=>1, duplicate_failure=>1, out_of_order=>1, duplicate_convergence=>1}, 'explains rejected records by reason');
my $late = parse_lines(event('packet_delivered', 20, node=>'A'), event('packet_delivered', 19, node=>'A'));
is($late->{delivered}, 1, 'backward timestamp not accepted for packets either');
my $no_newline = parse_lines(event('packet_delivered', 0, node=>'A'));
is($no_newline->{accepted}, 1, 'complete JSON accepted without final newline');
my $empty = parse_lines('');
ok(has_issues([$empty]), 'empty input does not look like success');
my $many = parse_lines(map { 'broken' } 1..100);
is($many->{rejected}, 100, 'all damaged lines counted');
is(scalar @{$many->{diagnostics}}, 20, 'diagnostic samples bounded');

my $quoted = parse_lines(event('packet_drop', 0, node=>'A', reason=>'bad,"header"'));
like(render_csv([$quoted]), qr/"bad,""header"""/, 'quotes and commas escaped for CSV');
$quoted->{source} = '=danger.csv';
like(render_csv([$quoted]), qr/"'=danger.csv"/, 'spreadsheet formula prefix neutralized');
my $two = render_text([$overlap, $overlap]);
is(() = $two =~ /a.*10 ms/g, 2, 'independent inputs preserved in report');

my $resynced = parse_lines('x' x 70000, event('packet_delivered', 0, node=>'A'));
is($resynced->{rejection_reasons}{line_too_long}, 1, 'oversized record rejected once');
is($resynced->{delivered}, 1, 'resumes at newline after oversized record');
my $oversized_eof = parse_lines('x' x 70000);
is($oversized_eof->{rejected}, 1, 'oversized unterminated record counted');
my $zero = parse_lines(event('link_failure', 0, failure_id=>'zero', link=>'A-B'),
    event('routes_converged', 0, failure_id=>'zero'));
is($zero->{failures}[0]{convergence_ms}, 0, 'same-timestamp recovery is valid');
my $failed_check = parse_lines(event('test_result', 0, name=>'probe', status=>'fail'));
ok(has_issues([$failed_check]), 'failed scenario alone triggers strict status');
my $incomplete = parse_lines(event('link_failure', 0, failure_id=>'lost', link=>'A-B'));
ok(has_issues([$incomplete]), 'incomplete recovery alone triggers strict status');
my $damaged_only = parse_lines('broken', event('packet_delivered', 0, node=>'A'));
ok(has_issues([$damaged_only]), 'damaged record alone triggers strict status');
my $utf8 = parse_lines(event('packet_drop', 0, node=>'A', reason=>"bad_\x{e9}"));
like(render_text([$utf8]), qr/bad_\x{e9}/, 'Unicode reason preserved');

done_testing();

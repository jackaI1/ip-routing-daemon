use strict;
use warnings;
use Test::More;
use File::Temp qw(tempdir);
use File::Spec;
use IPC::Open3;
use Symbol qw(gensym);
use Cwd qw(abs_path);

my $tmp = tempdir(CLEANUP => 1);
my $tool = abs_path('tools/routing-report.pl') || File::Spec->rel2abs('tools/routing-report.pl');
my $fixture = abs_path('t/fixtures/mixed.jsonl');
sub run_cli {
    my (@args) = @_;
    my $err = gensym;
    my $pid = open3(my $in, my $out, $err, $^X, $tool, @args);
    close $in;
    my $stdout = do { local $/; <$out> } // '';
    my $stderr = do { local $/; <$err> } // '';
    waitpid $pid, 0;
    return ($? >> 8, $stdout, $stderr);
}
sub slurp { open my $fh, '<', $_[0] or die $!; local $/; return <$fh>; }
my $text = "$tmp/report.txt";
my $csv = "$tmp/report.csv";
my @outputs = ('--text', $text, '--csv', $csv);
my ($status, $out, $err) = run_cli(@outputs, $fixture);
is($status, 0, 'tolerant mode writes partial report');
ok(-f $text && -f $csv, 'both report files written');
like(slurp($text), qr/f1.*35 ms/, 'CLI uses actual parser');
like(slurp($csv), qr/"rejected".*"invalid_json".*"2"/, 'CSV includes damaged record count');
is($out, '', 'reports go to requested files');
is($err, '', 'no Perl warnings');
($status) = run_cli('--strict', @outputs, $fixture);
is($status, 2, 'strict mode signals bad data and failed scenarios after writing reports');
($status, $out, $err) = run_cli(@outputs, "$tmp/missing.jsonl");
is($status, 1, 'unreadable input fails');
like($err, qr/Cannot open/, 'input error useful');
($status) = run_cli('--text', "$tmp/no-parent/out", '--csv', $csv, $fixture);
is($status, 1, 'missing output directory fails');
($status) = run_cli('--text', $text, '--csv', $text, $fixture);
is($status, 1, 'outputs cannot collide');
($status) = run_cli('--text', $fixture, '--csv', $csv, $fixture);
is($status, 1, 'cannot overwrite source log');
my $alias = "$tmp/alias.jsonl";
symlink($fixture, $alias) or die $!;
($status) = run_cli('--text', $alias, '--csv', $csv, $fixture);
is($status, 1, 'source symlink protected');
my $hard = "$tmp/hard.jsonl";
link($fixture, $hard) or die $!;
($status) = run_cli('--text', $hard, '--csv', $csv, $fixture);
is($status, 1, 'source hardlink protected');
($status) = run_cli(@outputs, $fixture, $alias);
is($status, 1, 'duplicate inputs cannot double counts');
($status, $out) = run_cli('--help');
is($status, 0, 'help succeeds');
like($out, qr/--strict/, 'help explains strict option');
($status) = run_cli('--nonsense');
is($status, 1, 'unknown options fail');
($status) = run_cli();
is($status, 1, 'missing arguments fail');

# The tool must find its library regardless of the caller's working directory.
my $cwd = abs_path('.');
chdir $tmp or die $!;
($status) = run_cli(@outputs, $fixture);
chdir $cwd or die $!;
is($status, 0, 'works outside repository');

done_testing();

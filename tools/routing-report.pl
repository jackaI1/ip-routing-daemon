#!/usr/bin/env perl
use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../lib";
use Routing::Report qw(read_log render_text render_csv has_issues);
use Getopt::Long qw(GetOptions);
use Cwd qw(abs_path);
use File::Basename qw(dirname basename);
use File::Spec;
use File::Temp qw(tempfile);
use Fcntl qw(O_WRONLY O_CREAT O_EXCL);
use Encode qw(decode);

my @reservations;
END {
    # Only unlink placeholders that still have the inode this invocation created.
    for my $owned (@reservations) {
        my @current = stat($owned->[0]);
        next unless @current && $current[0] == $owned->[1] && $current[1] == $owned->[2];
        unlink $owned->[0] or warn "Cannot release output reservation $owned->[0]: $!\n";
    }
}

sub usage {
    return <<'HELP';
Usage: perl tools/routing-report.pl --text REPORT.txt --csv REPORT.csv [--strict] LOG.jsonl [LOG2.jsonl ...]
Each log is an independent run. Reports are written even when input has damaged lines.
--strict exits 2 for rejected lines, incomplete recovery, empty logs or failed scenario checks.
Other exits: 0 = report written; 1 = usage or I/O error. Output parents must exist.
HELP
}

sub identity {
    my ($path) = @_;
    if (-e $path) {
        die "Not a regular file: $path\n" unless -f $path;
        my @metadata = stat($path);
        die "Cannot stat $path: $!\n" unless @metadata;
        return "inode:$metadata[0]:$metadata[1]";
    }
    die "Broken symlink: $path\n" if -l $path;
    my $parent = abs_path(dirname($path));
    die "Output directory does not exist: $path\n" unless defined $parent && -d $parent;
    return File::Spec->catfile($parent, basename($path));
}

sub main {
    my ($text_path, $csv_path, $strict, $help);
    Getopt::Long::Configure('no_auto_abbrev', 'no_ignore_case');
    return 1 unless GetOptions('text=s' => \$text_path, 'csv=s' => \$csv_path,
        'strict' => \$strict, 'help' => \$help);
    if ($help) { print usage(); return 0; }
    unless (defined $text_path && defined $csv_path && @ARGV) {
        print STDERR usage();
        return 1;
    }
    my @runs;
    my %input_ids;
    # Read everything before creating output, and protect aliases of source files.
    for my $path (@ARGV) {
        open my $input, '<:raw', $path or die "Cannot open $path: $!\n";
        my $id = identity($path);
        die "Duplicate input: $path\n" if $input_ids{$id}++;
        push @runs, read_log($input, decode('UTF-8', $path));
        close $input or die "Cannot close $path: $!\n";
    }
    my %output_ids;
    for my $path ($text_path, $csv_path) {
        if (!-e $path && !-l $path) {
            # Let the filesystem resolve case and Unicode-normalization aliases.
            sysopen my $reserved, $path, O_WRONLY | O_CREAT | O_EXCL, 0600
                or die "Cannot reserve output $path: $!\n";
            my @metadata = stat($reserved);
            push @reservations, [$path, @metadata[0, 1]];
            close $reserved or die "Cannot close reservation $path: $!\n";
        }
        my $id = identity($path);
        die "Output overlaps input or another output: $path\n" if $input_ids{$id} || $output_ids{$id}++;
    }
    my @staged;
    my @contents = (render_text(\@runs), render_csv(\@runs));
    for my $path ($text_path, $csv_path) {
        my ($fh, $temp) = tempfile('.routing-report-XXXXXX', DIR => dirname($path), UNLINK => 1);
        binmode $fh, ':encoding(UTF-8)' or die "Cannot set output encoding: $!\n";
        print {$fh} shift @contents or die "Cannot write $path: $!\n";
        close $fh or die "Cannot close $path: $!\n";
        push @staged, [$temp, $path];
    }
    for my $file (@staged) {
        rename $file->[0], $file->[1] or die "Cannot install report $file->[1]: $!\n";
    }
    return $strict && has_issues(\@runs) ? 2 : 0;
}

my $status = eval { main() };
if ($@) { print STDERR $@; exit 1; }
exit $status;

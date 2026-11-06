package Routing::Report;
use strict;
use warnings;
use Exporter 'import';
use JSON::PP qw(decode_json);
our @EXPORT_OK = qw(read_log render_text render_csv has_issues);

my %fields = (
    link_failure     => [qw(failure_id link)],
    routes_converged => [qw(failure_id)],
    packet_drop      => [qw(node reason)],
    malformed_packet => [qw(node reason)],
    packet_delivered => [qw(node)],
    test_result      => [qw(name status)],
);

sub _reject {
    my ($run, $line, $reason) = @_;
    $run->{rejected}++;
    $run->{rejection_reasons}{$reason}++;
    push @{$run->{diagnostics}}, {line => $line, reason => $reason}
        if @{$run->{diagnostics}} < 20;
}

sub _valid_text {
    my ($value) = @_;
    return defined($value) && !ref($value) && length($value) > 0
        && length($value) <= 200 && $value !~ /[\x00-\x1f\x7f]/;
}

sub _record {
    my ($run, $line, $number, $by_id) = @_;
    my $e = eval { decode_json($line) };
    if ($@ || ref($e) ne 'HASH') {
        _reject($run, $number, 'invalid_json');
        return;
    }
    my $kind = $e->{event};
    unless (_valid_text($kind) && exists $fields{$kind}
        && defined($e->{t_ms}) && !ref($e->{t_ms})
        && $e->{t_ms} =~ /\A(?:0|[1-9][0-9]{0,11})\z/
        && !grep { !_valid_text($e->{$_}) } @{$fields{$kind}}) {
        _reject($run, $number, 'invalid_event');
        return;
    }
    if ($kind eq 'test_result' && $e->{status} !~ /\A(?:pass|fail)\z/) {
        _reject($run, $number, 'invalid_event');
        return;
    }
    my $time = 0 + $e->{t_ms};
    if (defined($run->{last_ms}) && $time < $run->{last_ms}) {
        _reject($run, $number, 'out_of_order');
        return;
    }
    if ($kind eq 'link_failure') {
        if (exists $by_id->{$e->{failure_id}}) {
            _reject($run, $number, 'duplicate_failure');
            return;
        }
        my $failure = {failure_id => $e->{failure_id}, link => $e->{link}, start_ms => $time};
        push @{$run->{failures}}, $failure;
        $by_id->{$e->{failure_id}} = $failure;
    } elsif ($kind eq 'routes_converged') {
        my $failure = $by_id->{$e->{failure_id}};
        if (!$failure || defined $failure->{end_ms}) {
            _reject($run, $number, $failure ? 'duplicate_convergence' : 'orphan_convergence');
            return;
        }
        $failure->{end_ms} = $time;
        $failure->{convergence_ms} = $time - $failure->{start_ms};
    } elsif ($kind eq 'packet_drop' || $kind eq 'malformed_packet') {
        $run->{drops}{$kind}{$e->{reason}}++;
    } elsif ($kind eq 'packet_delivered') {
        $run->{delivered}++;
    } elsif ($kind eq 'test_result') {
        push @{$run->{tests}}, {name => $e->{name}, status => $e->{status}, t_ms => $time};
    }
    $run->{last_ms} = $time;
    $run->{accepted}++;
}

sub read_log {
    my ($fh, $source) = @_;
    my $run = {source => $source, accepted => 0, rejected => 0, delivered => 0,
        failures => [], drops => {}, tests => [], diagnostics => [], rejection_reasons => {}};
    my (%by_id, $buffer, $oversize, $number);
    $buffer = '';
    $number = 0;
    # Fixed-size reads prevent an unbroken corrupt line from filling memory.
    while (1) {
        my $size = read($fh, my $chunk, 8192);
        die "Cannot read $source: $!\n" unless defined $size;
        last if $size == 0;
        while (length $chunk) {
            my $newline = index($chunk, "\n");
            my $part = $newline < 0 ? $chunk : substr($chunk, 0, $newline);
            if (!$oversize) {
                $buffer .= $part;
                if (length($buffer) > 65536) { $oversize = 1; $buffer = ''; }
            }
            if ($newline < 0) { last; }
            $number++;
            if ($oversize) { _reject($run, $number, 'line_too_long'); }
            else { _record($run, $buffer, $number, \%by_id); }
            $buffer = '';
            $oversize = 0;
            $chunk = substr($chunk, $newline + 1);
        }
    }
    if ($oversize || length $buffer) {
        $number++;
        if ($oversize) { _reject($run, $number, 'line_too_long'); }
        else { _record($run, $buffer, $number, \%by_id); }
    }
    return $run;
}

sub has_issues {
    my ($runs) = @_;
    for my $run (@$runs) {
        return 1 if !$run->{accepted} || $run->{rejected}
            || grep { !defined $_->{end_ms} } @{$run->{failures}};
        return 1 if grep { $_->{status} eq 'fail' } @{$run->{tests}};
    }
    return 0;
}

sub render_text {
    my ($runs) = @_;
    my $text = "Routing report\nConvergence = observed table recovery time; an unreachable route may be correct.\n";
    for my $run (@$runs) {
        my $source = $run->{source};
        $source =~ s/[\x00-\x1f\x7f]/?/g;
        $text .= "\nSource: $source\nAccepted events: $run->{accepted}; rejected lines: $run->{rejected}; delivered packets: $run->{delivered}\n";
        $text .= "WARNING: no usable events\n" unless $run->{accepted};
        $text .= "Link failures:\n";
        $text .= "  none observed\n" unless @{$run->{failures}};
        for my $f (@{$run->{failures}}) {
            my $duration = defined $f->{convergence_ms} ? "$f->{convergence_ms} ms" : 'incomplete (no convergence event)';
            $text .= "  $f->{failure_id} [$f->{link}] at $f->{start_ms} ms: $duration\n";
        }
        $text .= "Dropped or malformed packets (category / reason / count):\n";
        $text .= "  none observed\n" unless keys %{$run->{drops}};
        for my $category (sort keys %{$run->{drops}}) {
            for my $reason (sort keys %{$run->{drops}{$category}}) {
                $text .= "  $category / $reason / $run->{drops}{$category}{$reason}\n";
            }
        }
        $text .= "Scenario checks:\n";
        $text .= "  none recorded\n" unless @{$run->{tests}};
        $text .= "  $_->{name}: $_->{status}\n" for @{$run->{tests}};
        if ($run->{rejected}) {
            $text .= "Rejected log lines (separate from packet counts):\n";
            $text .= "  $_: $run->{rejection_reasons}{$_}\n" for sort keys %{$run->{rejection_reasons}};
            $text .= "  line $_->{line}: $_->{reason}\n" for @{$run->{diagnostics}};
            $text .= "  diagnostic samples limited to 20\n" if $run->{rejected} > 20;
        }
    }
    return $text;
}

sub _csv_cell {
    my ($value) = @_;
    $value = '' unless defined $value;
    # Avoid formula execution if a user opens an untrusted log in a spreadsheet.
    $value = "'$value" if $value =~ /\A\s*[=+\-@]/;
    $value =~ s/"/""/g;
    return qq{"$value"};
}

sub render_csv {
    my ($runs) = @_;
    my @columns = qw(record_type source failure_id link start_ms end_ms convergence_ms status category reason count test_name line);
    my @rows;
    for my $run (@$runs) {
        push @rows, {record_type => 'summary', source => $run->{source}, category => $_, count => $run->{$_}}
            for qw(accepted rejected delivered);
        for my $f (@{$run->{failures}}) {
            push @rows, {%$f, record_type => 'convergence', source => $run->{source},
                status => defined($f->{end_ms}) ? 'converged' : 'incomplete'};
        }
        for my $category (sort keys %{$run->{drops}}) {
            push @rows, {record_type => 'packet', source => $run->{source}, category => $category,
                reason => $_, count => $run->{drops}{$category}{$_}} for sort keys %{$run->{drops}{$category}};
        }
        push @rows, {record_type => 'test', source => $run->{source}, test_name => $_->{name}, status => $_->{status}}
            for @{$run->{tests}};
        push @rows, {record_type => 'rejected', source => $run->{source}, reason => $_, count => $run->{rejection_reasons}{$_}}
            for sort keys %{$run->{rejection_reasons}};
        push @rows, {record_type => 'diagnostic', source => $run->{source}, %$_} for @{$run->{diagnostics}};
    }
    return join('', map { join(',', map { _csv_cell($_) } @$_) . "\n" }
        \@columns, map { my $row = $_; [map { $row->{$_} } @columns] } @rows);
}

1;
